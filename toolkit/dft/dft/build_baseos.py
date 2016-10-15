#
# The contents of this file are subject to the Apache 2.0 license you may not
# use this file except in compliance with the License.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
#
# Copyright 2016 DFT project (http://www.debianfirmwaretoolkit.org).  
# All rights reserved. Use is subject to license terms.
#
# Debian Firmware Toolkit is the new name of Linux Firmware From Scratch
# Copyright 2014 LFFS project (http://www.linuxfirmwarefromscratch.org).  
#
#
# Contributors list :
#
#    William Bonnet     wllmbnnt@gmail.com, wbonnet@theitmakers.com
#
#

import logging, os, subprocess, tarfile, shutil, tempfile, distutils
from datetime import datetime
from distutils import dir_util, file_util

#
#    Class BuildBaseOS
#
class BuildBaseOS: 
    """This class implements method needed to create the base OS

       The "base OS" is the initial installation of Debian (debootstrap) which
       is used to apply ansible playbooks.

       The methods implemented in this class provides what is needed to :
         . create the debootstrap (chrooted environment)
         . handle filesystems like dev and proc in the chrooted environment
         . copy DFT and project specific templates into /dft_bootstrap
         . run ansible in the chroot
         . cleanup things when installation is done
    """



    # -------------------------------------------------------------------------
    #
    # __init__
    #
    # -------------------------------------------------------------------------
    def __init__(self):
        """Default constructor
        """

        # Default mirror to use. It has to be the URL of a valid Debian mirror
        # It is used by debootstrap as its sources of packages.
        self.pkg_archive_url = "http://mirrors/debian/"

        # TODO Temporary until file parsing is fixed
        self.debian_mirror_url  = "http://mirrors/"

        # Target version to use when building the debootstrap. It has to be
        # a Debian version (jessie, stretch, etc.)
        self.target_version = "stretch"

        # Stores the target architecture
        # TODO should we have a list here ? 
        self.target_arch = "amd64"

        # Retrieve the architecture of the host
        self.host_arch = subprocess.check_output("dpkg --print-architecture", shell=True).decode('UTF-8').rstrip()

        # Boolean used to flag the use of QEMU static
        self.use_qemu_static =  (self.host_arch != self.target_arch)

        # Debootstrap target to use (minbase or buildd)
        self.debootstrap_target = "minbase"

        # Boolean used to flag if the cache archive should used instead 
        # of doing a real debootstrap installation
        self.use_rootfs_cache = False

        # Boolean used to flag if the cache archive should used updated
        # after doing a real debootstrap installation
        self.update_rootfs_cache = False

        # Boolean used to flag if the cache archive is available. This value 
        # is set by the setup_configuration method. Default is False, to 
        # ensure it will be rebuild
        self.rootfs_cache_is_available = False

        # Timestamp is generated by the setup_configuration method
        self.timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        # Generates the path to the rootfs mountpoint
        rootfs_base_workdir = "/tmp/rootfs_mountpoint"
        rootfs_image_name   = self.target_arch + "-" + self.target_version + "-" + self.timestamp

        # Stores the path to the rootfs mountpoint used by debootstrap
        self.rootfs_mountpoint = rootfs_base_workdir + "/" + rootfs_image_name
  
        # Path to the directory ued to store cache archives
        self.rootfs_generator_cachedir = "/tmp"

        # Current log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        self.log_level = "DEBUG"

        # Name of the current baseos being produced. Used in rootfs mount
        # point path and archive name generation
        self.target_name = "test"

        # Generate the cache archive filename
        self.archive_filename = self.rootfs_generator_cachedir + "/" + self.target_arch + "-" +  self.target_version + "-" +  self.target_name + ".tar"

        # Flags used to remove 'mount bind' states
        self.proc_is_mounted   = False
        self.devpts_is_mounted = False
        self.devshm_is_mounted = False

        logging.basicConfig(level=self.log_level)

        # 
        self.dft_source_path = "/home/william/Devel/dft/toolkit/ansible"
        self.dft_additional_path =  [ "/tmp/dft" ]
        self.dft_ansible_targets = [ "test" ]

    # -------------------------------------------------------------------------
    #
    # install_baseos
    #
    # -------------------------------------------------------------------------
    def install_baseos(self):
        """This method implement the logic of generating the rootfs. It calls
        dedicated method for each step. The main steps are :

        . setting up configuration
        . extracting cache archive content or running debootstrap
        . setup QEMU and run stage 2 if needed
        . update cache if needed
        . deploy DFT Ansible templates, and run Ansible to do confiugration
        . cleanup installation files
        . cleanup QEMU
        """

        # Check that DFT path is valid
        if os.path.isdir(self.dft_source_path) == False:
            logging.critical("Path to DFT installation is not valid : %s",  self.dft_source_path)
            exit(1)

        # Ensure target rootfs mountpoint exists and is a dir
        if os.path.isdir(self.rootfs_mountpoint) == False:
            os.makedirs(self.rootfs_mountpoint)
        else:
            logging.warn("target rootfs mount point already exists : " + self.rootfs_mountpoint)

        # Check if the archive has to be used instead of doing a debootstraping
        # for real. Only if the archive exist...
        if self.use_rootfs_cache == True and self.rootfs_cache_is_available == True:
            self.fake_generate_debootstrap_rootfs()
        else:
            # In any other cases, do a real debootstrap call
            self.generate_debootstrap_rootfs()

        # þest the archive has to be updated
        if self.update_rootfs_cache == True:
            # But only do it if we haven't bee using the cache, or it
            # would be extracted, then archived again.
            if self.use_rootfs_cache == True:
                self.update_rootfs_archive()

        # Launch Ansible to install roles identified in configuration file
        self.install_packages()

        # Once installation has been played, we need to do some cleanup
        # like ensute that no mount bind is still mounted, or delete the
        # DFT ansible files
        self.cleanup_installation_files()

        # Remove QEMU if it has been isntalled. It has to be done in the end
        # since some cleanup tasks could need QEMU
        if self.use_qemu_static == True:
            self.cleanup_qemu()



    # -------------------------------------------------------------------------
    #
    # run_ansible
    #
    # -------------------------------------------------------------------------
    def install_packages(self):
        """This method remove the QEMU static binary which has been previously 
        copied to the target 
        """

        logging.info("installing packages...")

        # Create the target directory. DFT files will be installed under this
        # directory.
        try:
            logging.debug("copying DFT toolkit...")

            # Create the target directory in the rootfs
            dft_target_path = self.rootfs_mountpoint + "/dft_bootstrap/"
            if not os.path.exists(dft_target_path):
                os.makedirs(dft_target_path)

            # Copy the DFT toolkit content to the target rootfs
            for target_to_copy in os.listdir(self.dft_source_path):
                target_to_copy_path = os.path.join(self.dft_source_path, target_to_copy)
                if os.path.isfile(target_to_copy_path):
                    logging.debug("copying file " + target_to_copy_path + " => " + dft_target_path)
                    distutils.file_util.copy_file(target_to_copy_path, dft_target_path)
                else:
                    logging.debug("copying tree " + target_to_copy_path + " => " + dft_target_path)
                    distutils.dir_util.copy_tree(target_to_copy_path, os.path.join(dft_target_path, target_to_copy))

            # Copy the DFT toolkit content to the target rootfs
            for additional_path in self.dft_additional_path:
                for target_to_copy in os.listdir(additional_path):
                    target_to_copy_path = os.path.join(additional_path, target_to_copy)
                    if os.path.isfile(target_to_copy_path):
                        logging.debug("copying file " + target_to_copy_path + " => " + dft_target_path)
                        distutils.file_util.copy_file(target_to_copy_path, dft_target_path)
                    else:
                        logging.debug("copying tree " + target_to_copy_path + " => " + dft_target_path)
                        distutils.dir_util.copy_tree(target_to_copy_path, os.path.join(dft_target_path, target_to_copy))

        except OSError as e:
            # Call clean up to umount /proc and /dev
            self.cleanup_installation_files()
            logging.critical("Error: %s - %s." % (e.filename, e.strerror))
            exit(1)

        except shutil.Error as e:
            self.cleanup_installation_files()
            logging.critical("Error: %s - %s." % (e.filename, e.strerror))
            exit(1)
     
        # Copy the project roles to the target rootfs

        # Execute Ansible
        logging.info("running ansible...")
        for ansible_target in self.dft_ansible_targets:
            sudo_command = "LANG=C sudo chroot " + self.rootfs_mountpoint + " /usr/bin/ansible-playbook -i inventory.yml -c local " + ansible_target + ".yml"
            logging.info("running ansible playbook : " + sudo_command)
            subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)



    # -------------------------------------------------------------------------
    #
    # setup_qemu
    #
    # -------------------------------------------------------------------------
    def setup_qemu(self):
        """This method remove the QEMU static binary which has been previously 
        copied to the target 
        """

        # We should not execute if the flag is not set. Should have already 
        # been tested, but double check by security
        if self.use_qemu_static != True:
            return

        # Copy the QEMU binary to the target, using root privileges
        if   self.target_arch == "armhf":     qemu_target_arch = "arm"
        elif self.target_arch == "armel":     qemu_target_arch = "arm"
        else:                                 qemu_target_arch = self.target_arch

        logging.info("setting up QEMU for arch " + self.target_arch + " (using /usr/bin/qemu-" + qemu_target_arch + "-static)")
        sudo_command = "sudo cp /usr/bin/qemu-" + qemu_target_arch + "-static " + self.rootfs_mountpoint + "/usr/bin/"
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)



    # -------------------------------------------------------------------------
    #
    # cleanup_qemu
    #
    # -------------------------------------------------------------------------
    def cleanup_qemu(self):
        """This method copy the QEMU static binary to the target 
        """

        # We should not execute if the flag is not set. Should have already 
        # been tested, but double check by security
        if self.use_qemu_static != True:
            return

        # Copy the QEMU binary to the target, using root privileges
        if   self.target_arch == "armhf":     qemu_target_arch = "arm"
        elif self.target_arch == "armel":     qemu_target_arch = "arm"
        else:                                 qemu_target_arch = self.target_arch
        
        # Execute the file removal with root privileges
        logging.info("cleaning QEMU for arch " + self.target_arch + "(/usr/bin/qemu-" + qemu_target_arch + "-static)")
        os.system("sudo rm " + self.rootfs_mountpoint + "/usr/bin/qemu-" + qemu_target_arch + "-static")



    # -------------------------------------------------------------------------
    #
    # cleanup_installation_files
    #
    # -------------------------------------------------------------------------
    def cleanup_installation_files(self):
        """This method is incharge of cleaning processes after Ansible has been 
        launched. In some case some daemons are still running inside the 
        chroot, and they have to be stopped manually, or even killed in order
        to be able to umount /dev/ and /proc from inside the chroot
        """
        logging.info("starting to cleanup installation files")

        # Check if /proc is mounted, then umount it
        if self.proc_is_mounted == True:
            sudo_command = "sudo umount " + self.rootfs_mountpoint + "/dev/pts"
            logging.debug("running : " + sudo_command)
            subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)

        # Check if /dev/shm is mounted, then umount it
        if self.devshm_is_mounted == True:
            sudo_command = "sudo umount " + self.rootfs_mountpoint + "/dev/shm"
            logging.debug("running : " + sudo_command)
            subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)

        # Check if /dev/pts is mounted, then umount it
        if self.devpts_is_mounted == True:
            sudo_command = "sudo umount " + self.rootfs_mountpoint + "/proc"
            logging.debug("running : " + sudo_command)
            subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)

        # Delete the DFT files from the rootfs
#        shutil.rmtree(self.rootfs_mountpoint + "/dft_bootstrap")



    # -------------------------------------------------------------------------
    #
    # generate_build_number
    #
    # -------------------------------------------------------------------------
    def generate_build_number(self):
        """ Generate a version number in /etc/dft_version file. This is used
        to keep track of generation date.
        """

        logging.info("starting to generate build number")

        # Open the file and writes the timestamp in it
        filepath = self.rootfs_mountpoint + "/etc/dft_version"
        with tempfile.NamedTemporaryFile(mode='w+', delete=False) as f:
            f.write("DFT-" + self.timestamp + "\n")
        f.close()

        sudo_command = "sudo mv -f " + f.name + " " + filepath
        logging.debug("running : " + sudo_command)
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)
   

    # -------------------------------------------------------------------------
    #
    # update_rootfs_archive
    #
    # -------------------------------------------------------------------------
    def update_rootfs_archive(self):
        """This methods update (delete then recreate) the rootfs archive after
        doing a real debootstrap installation.

        Archive is not updated if cache has been used instead of debootstraping
        otherwise it would generate the same archive"""
        logging.info("starting to update rootfs archive")

        # Remove existing archive before generating the new one
        try:
            if os.path.isfile(self.archive_filename) == True:
                logging.info("removing previous archive file : " + self.archive_filename)
                os.remove(self.archive_filename)

        # Catch file removal exceptions
        except OSError as e:
            logging.critical("Error: %s - %s." % (e.filename, e.strerror))
            exit(1)

        # Create the new archive
        cache_archive = tarfile.open(self.archive_filename)
        cache_archive.add(name=self.rootfs_mountpoint)
        cache_archive.close()



    # -------------------------------------------------------------------------
    #
    # fake_generate_debootstrap_rootfs
    #
    # -------------------------------------------------------------------------
    def fake_generate_debootstrap_rootfs(self):
        logging.info("starting to fake generate debootstrap rootfs")

        # Check that the archive exists
        if os.path.isfile(self.archive_filename) == False:
            logging.warning("cache has been activate and archive file does not exist : " + self.archive_filename)
            return False

        # Extract tar file to rootfs mountpoint
        logging.info("extracting archive : " + self.archive_filename)
        cache_archive = tarfile.open(self.archive_filename)
        cache_archive.extractall(path=self.rootfs_mountpoint)
        cache_archive.close()



    # -------------------------------------------------------------------------
    #
    # generate_debootstrap_rootfs
    #
    # -------------------------------------------------------------------------
    def generate_debootstrap_rootfs(self):
        """
        """

        logging.info("starting to generate debootstrap rootfs")

        # Generate the base debootstrap command
        debootstrap_command  = "sudo debootstrap --no-check-gpg"

        # Add the foreign and arch only if they are different from host, and
        # thus if use_qemu_static is True
        if self.use_qemu_static == True:
            logging.info("running debootstrap stage 1")
            debootstrap_command += " --foreign --arch=" + self.target_arch 
        else:
            logging.info("running debootstrap")

        # Add the target, mount point and repository url to the debootstrap command
        debootstrap_command += " " +  self.target_version + " " + self.rootfs_mountpoint + " " + self.pkg_archive_url

        # Finally run the subprocess
        logging.debug("running : " + debootstrap_command)
        subprocess.run(debootstrap_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)

        # Check if we are working with foreign arch, then ... 
        if self.use_qemu_static == True:
            # QEMU is used, and we have to install it into the target
            self.setup_qemu()

            # And second stage must be run
            logging.info("doing debootstrap stage 2")
            logging.debug("running : " + debootstrap_command)
            debootstrap_command  = "LANG=C sudo chroot " + self.rootfs_mountpoint + " /debootstrap/debootstrap --second-stage"
            subprocess.run(debootstrap_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)


        # Mount bind /proc into the rootfs mountpoint
        sudo_command = "sudo mount --bind --make-rslave /proc " + self.rootfs_mountpoint + "/proc"
        logging.debug("running : " + sudo_command)
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)
        self.proc_is_mounted = True

        # Mount bind /dev/pts into the rootfs mountpoint
        sudo_command = "sudo mount --bind --make-rslave /dev/pts " + self.rootfs_mountpoint + "/dev/pts"
        logging.debug("running : " + sudo_command)
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)
        self.devpts_is_mounted = True

        # Mount bind /dev/shm into the rootfs mountpoint
        sudo_command = "sudo mount --bind --make-rslave /dev/shm " + self.rootfs_mountpoint + "/dev/shm"
        logging.debug("running : " + sudo_command)
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)
        self.devshm_is_mounted = True

        # Update the APT sources
        self.generate_apt_sources_configuration()

        # Then update the list of packages
        apt_command = "sudo chroot " + self.rootfs_mountpoint + " /usr/bin/apt-get update"
        logging.debug("running : " + apt_command)
        subprocess.run(apt_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)
 
        # Install extra packages into the chroot
        apt_command = "sudo chroot " + self.rootfs_mountpoint + " /usr/bin/apt-get install --no-install-recommends --yes --allow-unauthenticated apt-utils ansible"
        logging.debug("running : " + apt_command)
        subprocess.run(apt_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)

        # Generate a unique build timestamp into /etc/dft_version 
        self.generate_build_number()



    # -------------------------------------------------------------------------
    #
    # generate_apt_sources_configuration
    #
    # -------------------------------------------------------------------------
    def generate_apt_sources_configuration(self):
        """ This method has two functions, configure APT sources and configure
        apt to ignore validity check on expired repositories

        The method generates a file named 10no-check-valid-until which is 
        placed in the apt config directory. It is used to deactivate validity
        check on repository during installation, so a mirror can still be used
        even if it is expired. This use case happens often when mirrors cannot
        be sync'ed automatically from the internet

        Second part of the methods iterate the repositories from configuration
        file and generates sources.list
        """
        #TODO : remove validity check after generation ? => flag ? 
        logging.info("starting to generate APT sources configuration")

        # Generate the file path
        filepath = self.rootfs_mountpoint + "/etc/apt/apt.conf.d/10no-check-valid-until"

        # Open the file and writes configuration in it
        with tempfile.NamedTemporaryFile(mode='w+', delete=False) as f:
            f.write("Acquire::Check-Valid-Until \"0\";\n")
        f.close()

        sudo_command = "sudo mv -f " + f.name + " " + filepath
        logging.debug("running : " + sudo_command)
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)

        # Generate the file path
        filepath = self.rootfs_mountpoint + "/etc/apt/sources.list"

        # Open the file and writes configuration in it
        with tempfile.NamedTemporaryFile(mode='w+', delete=False) as f:
            f.write("deb " + self.debian_mirror_url + "/debian " + self.target_version + " main contrib non-free\n")
            f.write("deb " + self.debian_mirror_url + "/debian-security " + self.target_version + "/updates main contrib non-free\n")
            f.write("deb " + self.debian_mirror_url + "/debian " + self.target_version + "-updates main contrib non-free\n")
        f.close()

        sudo_command = "sudo mv -f " + f.name + " " + filepath
        logging.debug("running : " + sudo_command)
        subprocess.run(sudo_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, check=True)