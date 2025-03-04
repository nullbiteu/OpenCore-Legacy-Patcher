# Framework for mounting and patching macOS root volume
# Copyright (C) 2020-2021, Dhinak G, Mykola Grymalyuk
# Missing Features:
# - Full System/Library Snapshotting (need to research how Apple achieves this)
#   - Temporary Work-around: sudo bless --mount /System/Volumes/Update/mnt1 --bootefi --last-sealed-snapshot
# - Work-around battery throttling on laptops with no battery (IOPlatformPluginFamily.kext/Contents/PlugIns/ACPI_SMC_PlatformPlugin.kext/Contents/Resources/)

import os
import plistlib
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from Resources import Constants, DeviceProbe, ModelArray, PCIIDArray, Utilities


class PatchSysVolume:
    def __init__(self, model, versions):
        self.model = model
        self.constants: Constants.Constants = versions
        self.root_mount_path = None
        self.sip_enabled = True
        self.sbm_enabled = True
        self.amfi_enabled = True
        self.fv_enabled = True
        self.nvidia_legacy = False
        self.amd_ts1 = False
        self.amd_ts2 = False
        self.iron_gpu = False
        self.sandy_gpu = False
        self.ivy_gpu = False
        self.nvidia_legacy = False
        self.brightness_legacy = False
        self.legacy_audio = False
        self.added_kexts = False
        self.amfi_must_disable = False
        self.no_patch = True

        if self.constants.detected_os > self.constants.catalina:
            # Big Sur and newer use APFS snapshots
            self.mount_location = "/System/Volumes/Update/mnt1"
        else:
            self.mount_location = "/"
        self.mount_extensions = f"{self.mount_location}/System/Library/Extensions"
        self.mount_frameworks = f"{self.mount_location}/System/Library/Frameworks"
        self.mount_lauchd = f"{self.mount_location}/System/Library/LaunchDaemons"
        self.mount_private_frameworks = f"{self.mount_location}/System/Library/PrivateFrameworks"

    def elevated(self, *args, **kwargs) -> subprocess.CompletedProcess([Any], returncode=0):
        if os.getuid() == 0:
            return subprocess.run(*args, **kwargs)
        else:
            return subprocess.run(["sudo"] + [args[0][0]] + args[0][1:], **kwargs)

    def find_mount_root_vol(self, patch):
        self.root_mount_path  = Utilities.get_disk_path()
        if self.root_mount_path.startswith("disk"):
            print(f"- Found Root Volume at: {self.root_mount_path}")
            if Path(self.mount_extensions).exists():
                print("- Root Volume is already mounted")
                if patch is True:
                    self.patch_root_vol()
                    return True
                else:
                    self.unpatch_root_vol()
                    return True
            else:
                print("- Mounting drive as writable in OS")
                self.elevated(["mount", "-o", "nobrowse", "-t", "apfs", f"/dev/{self.root_mount_path}", self.mount_location], stdout=subprocess.PIPE).stdout.decode().strip().encode()
                if Path(self.mount_extensions).exists():
                    print("- Successfully mounted the Root Volume")
                    if patch is True:
                        self.patch_root_vol()
                        return True
                    else:
                        self.unpatch_root_vol()
                        return True
                else:
                    print("- Failed to mount the Root Volume")
                    print("- Recommend rebooting the machine and trying to patch again")
                    input("- Press [ENTER] to exit: ")
        else:
            print("- Could not find root volume")
            input("- Press [ENTER] to exit: ")

    def unpatch_root_vol(self):
        print("- Reverting to last signed APFS snapshot")
        self.elevated(["bless", "--mount", self.mount_location, "--bootefi", "--last-sealed-snapshot"], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def rebuild_snapshot(self):
        if self.constants.gui_mode is False:
            input("Press [ENTER] to continue with cache rebuild: ")
        print("- Rebuilding Kernel Cache (This may take some time)")
        result = self.elevated(["kmutil", "install", "--volume-root", self.mount_location, "--update-all"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        if result.returncode != 0:
            self.success_status = False
            print("- Unable to build new kernel cache")
            print("\nPlease report this to Github")
            print("Reason for Patch Failure:")
            print(result.stdout.decode())
            print("")
            print("\nPlease reboot the machine to avoid potential issues rerunning the patcher")
            input("Press [ENTER] to continue")
        else:
            self.success_status = True
            print("- Successfully built new kernel cache")
            if self.constants.gui_mode is False:
                input("Press [ENTER] to continue with snapshotting")
            print("- Creating new APFS snapshot")
            self.elevated(["bless", "--folder", f"{self.mount_location}/System/Library/CoreServices", "--bootefi", "--create-snapshot"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
            self.unmount_drive()
            print("- Patching complete")
            print("\nPlease reboot the machine for patches to take effect")
            input("Press [ENTER] to continue")


    def unmount_drive(self):
        print("- Unmounting Root Volume (Don't worry if this fails)")
        self.elevated(["diskutil", "unmount", self.root_mount_path], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def delete_old_binaries(self, vendor_patch):
        for delete_current_kext in vendor_patch:
            delete_path = Path(self.mount_extensions) / Path(delete_current_kext)
            if Path(delete_path).exists():
                print(f"- Deleting {delete_current_kext}")
                self.elevated(["sudo", "rm", "-R", delete_path], stdout=subprocess.PIPE).stdout.decode().strip().encode()
            else:
                print(f"- Couldn't find {delete_current_kext}, skipping")

    def add_new_binaries(self, vendor_patch, vendor_location):
        for add_current_kext in vendor_patch:
            existing_path = Path(self.mount_extensions) / Path(add_current_kext)
            if Path(existing_path).exists():
                print(f"- Found conflicting kext, Deleting Root Volume's {add_current_kext}")
                self.elevated(["rm", "-R", existing_path], stdout=subprocess.PIPE).stdout.decode().strip().encode()
            print(f"- Adding {add_current_kext}")
            self.elevated(["cp", "-R", f"{vendor_location}/{add_current_kext}", self.mount_extensions], stdout=subprocess.PIPE).stdout.decode().strip().encode()
            self.elevated(["chmod", "-Rf", "755", f"{self.mount_extensions}/{add_current_kext}"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
            self.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_extensions}/{add_current_kext}"], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def add_brightness_patch(self):
        self.delete_old_binaries(ModelArray.DeleteBrightness)
        self.add_new_binaries(ModelArray.AddBrightness, self.constants.legacy_brightness)
        self.elevated(["ditto", self.constants.payload_apple_private_frameworks_path_brightness, self.mount_private_frameworks], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        self.elevated(["chmod", "-Rf", "755", f"{self.mount_private_frameworks}/DisplayServices.framework"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        self.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_private_frameworks}/DisplayServices.framework"], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def add_audio_patch(self):
        self.delete_old_binaries(ModelArray.DeleteVolumeControl)
        self.add_new_binaries(ModelArray.AddVolumeControl, self.constants.audio_path)

    def gpu_accel_legacy_nvidia(self):
        self.delete_old_binaries(ModelArray.DeleteNvidiaAccel11)
        self.add_new_binaries(ModelArray.AddGeneralAccel, self.constants.legacy_general_path)
        self.add_new_binaries(ModelArray.AddNvidiaAccel11, self.constants.legacy_nvidia_path)

    def gpu_framebuffer_legacy_nvidia(self):
        self.add_new_binaries(ModelArray.AddNvidiaBrightness, self.constants.legacy_nvidia_path)

    def gpu_accel_legacy_ts1(self):
        self.delete_old_binaries(ModelArray.DeleteAMDAccel11)
        self.add_new_binaries(ModelArray.AddGeneralAccel, self.constants.legacy_general_path)
        self.add_new_binaries(ModelArray.AddAMDAccel11, self.constants.legacy_amd_path)

    def gpu_accel_legacy_ts2(self):
        self.delete_old_binaries(ModelArray.DeleteAMDAccel11)
        self.delete_old_binaries(ModelArray.DeleteAMDAccel11TS2)
        self.add_new_binaries(ModelArray.AddGeneralAccel, self.constants.legacy_general_path)
        self.add_new_binaries(ModelArray.AddAMDAccel11, self.constants.legacy_amd_path)

    def gpu_framebuffer_legacy_amd(self):
        self.add_new_binaries(ModelArray.AddAMDBrightness, self.constants.legacy_amd_path)

    def gpu_accel_legacy_ironlake(self):
        self.delete_old_binaries(ModelArray.DeleteNvidiaAccel11)
        self.add_new_binaries(ModelArray.AddGeneralAccel, self.constants.legacy_general_path)
        self.add_new_binaries(ModelArray.AddIntelGen1Accel, self.constants.legacy_intel_gen1_path)

    def gpu_framebuffer_legacy_ironlake(self):
        self.add_new_binaries(ModelArray.AddIntelGen1Accel, self.constants.legacy_intel_gen1_path)

    def gpu_accel_legacy_sandybridge(self):
        self.delete_old_binaries(ModelArray.DeleteNvidiaAccel11)
        self.add_new_binaries(ModelArray.AddGeneralAccel, self.constants.legacy_general_path)
        self.add_new_binaries(ModelArray.AddIntelGen2Accel, self.constants.legacy_intel_gen2_path)

    def gpu_framebuffer_legacy_sandybridge(self):
        self.add_new_binaries(ModelArray.AddIntelGen2Accel, self.constants.legacy_intel_gen1_path)

    def gpu_framebuffer_ivybridge(self):
        self.delete_old_binaries(ModelArray.DeleteAMDAccel11)
        self.add_new_binaries(ModelArray.AddIntelGen3Accel, self.constants.legacy_intel_gen3_path)
        self.elevated(["ditto", self.constants.payload_apple_frameworks_path_accel, self.mount_frameworks], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def gpu_accel_legacy_extended(self):
        print("- Merging general legacy Frameworks")
        self.elevated(["ditto", self.constants.payload_apple_frameworks_path_accel, self.mount_frameworks], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        if Path(self.mount_lauchd / Path("HiddHack.plist")).exists():
            print("- Removing legacy HiddHack")
            self.elevated(["rm", f"{self.mount_lauchd}/HiddHack.plist"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        print("- Adding IOHID-Fixup.plist")
        self.elevated(["ditto", self.constants.payload_apple_lauchd_path_accel, self.mount_lauchd], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        self.elevated(["chmod", "755", f"{self.mount_lauchd}/IOHID-Fixup.plist"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        self.elevated(["chown", "root:wheel", f"{self.mount_lauchd}/IOHID-Fixup.plist"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        print("- Merging general legacy PrivateFrameworks")
        self.elevated(["ditto", self.constants.payload_apple_private_frameworks_path_accel, self.mount_private_frameworks], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def gpu_accel_legacy_extended_ts2(self):
        print("- Merging TeraScale 2 legacy Frameworks")
        self.elevated(["ditto", self.constants.payload_apple_frameworks_path_accel_ts2, self.mount_frameworks], stdout=subprocess.PIPE).stdout.decode().strip().encode()
        print("- Merging TeraScale 2 PrivateFrameworks")
        self.elevated(["ditto", self.constants.payload_apple_private_frameworks_path_accel_ts2, self.mount_private_frameworks], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def patch_root_vol(self):
        print(f"- Running patches for {self.model}")
        # Graphics patches
        if self.nvidia_legacy is True:
            print("- Installing legacy Nvidia Patches")
            if self.constants.detected_os == self.constants.big_sur:
                print("- Detected Big Sur, installing Acceleration patches")
                self.gpu_accel_legacy_nvidia()
                self.added_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
                self.gpu_framebuffer_legacy_nvidia()

        if self.amd_ts1 is True:
            print("- Installing legacy TeraScale 1 Patches")
            if self.constants.detected_os == self.constants.big_sur:
                print("- Detected Big Sur, installing Acceleration patches")
                self.gpu_accel_legacy_ts1()
                self.added_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
                self.gpu_framebuffer_legacy_amd()

        if self.amd_ts2 is True:
            print("- Installing legacy TeraScale 2 Patches")
            if self.constants.detected_os == self.constants.big_sur:
                print("- Detected Big Sur, installing Acceleration patches")
                self.gpu_accel_legacy_ts2()
                self.added_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
                self.gpu_framebuffer_legacy_amd()

        if self.iron_gpu is True:
            print("- Installing legacy Ironlake Patches")
            if self.constants.detected_os == self.constants.big_sur:
                print("- Detected Big Sur, installing Acceleration patches")
                self.gpu_accel_legacy_ironlake()
                self.added_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
                self.gpu_framebuffer_legacy_ironlake()

        if self.sandy_gpu is True:
            print("- Installing legacy Sandy Bridge Patches")
            if self.constants.detected_os == self.constants.big_sur:
                print("- Detected Big Sur, installing Acceleration patches")
                self.gpu_accel_legacy_sandybridge()
                self.added_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
                self.gpu_framebuffer_legacy_sandybridge()

        if self.ivy_gpu is True:
            print("- Installing Ivy Bridge Patches")
            self.gpu_framebuffer_ivybridge()

        if self.amd_ts2 is True:
            # TeraScale 2 patches must be installed after Intel HD3000
            self.add_new_binaries(ModelArray.AddAMDAccel11TS2, self.constants.legacy_amd_path_ts2)

        if self.added_kexts is True:
            self.gpu_accel_legacy_extended()
            if self.amd_ts2 is True:
                self.gpu_accel_legacy_extended_ts2()

        # Misc patches
        if self.brightness_legacy is True:
            print("- Installing legacy Brightness Control")
            self.add_brightness_patch()

        if self.legacy_audio is True:
            print("- Fixing Volume Control Support")
            self.add_audio_patch()

        self.rebuild_snapshot()

    def check_files(self):
        if Path(self.constants.payload_apple_root_path).exists():
            print("- Found Apple Binaries")
            if self.constants.gui_mode is False:
                patch_input = input("Would you like to redownload?(y/n): ")
                if patch_input in {"y", "Y", "yes", "Yes"}:
                    shutil.rmtree(Path(self.constants.payload_apple_root_path))
                    self.download_files()
            else:
                self.download_files()
        else:
            print("- Apple binaries missing")
            self.download_files()

    def download_files(self):
        Utilities.cls()
        print("- Downloading Apple binaries")
        popen_oclp = subprocess.Popen(
            ["curl", "-S", "-L", f"{self.constants.url_apple_binaries}{self.constants.payload_version}.zip", "--output", self.constants.payload_apple_root_path_zip],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        for stdout_line in iter(popen_oclp.stdout.readline, ""):
            print(stdout_line, end="")
        popen_oclp.stdout.close()
        if self.constants.payload_apple_root_path_zip.exists():
            print("- Download completed")
            print("- Unzipping download...")
            try:
                subprocess.run(["unzip", self.constants.payload_apple_root_path_zip], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.constants.payload_path).stdout.decode()
                print("- Renaming folder")
                os.rename(self.constants.payload_apple_root_path_unzip, self.constants.payload_apple_root_path)
                print("- Binaries downloaded to:")
                print(self.constants.payload_path)
                if self.constants.gui_mode is False:
                    input("Press [ENTER] to continue")
            except zipfile.BadZipFile:
                print("- Couldn't unzip")
            os.remove(self.constants.payload_apple_root_path_zip)
        else:
            print("- Download failed, please verify the below link works:")
            print(f"{self.constants.url_apple_binaries}{self.constants.payload_version}")


    def detect_gpus(self):
        igpu_vendor, igpu_device, igpu_acpi = DeviceProbe.pci_probe().gpu_probe("IGPU")
        dgpu_vendor, dgpu_device, dgpu_acpi = DeviceProbe.pci_probe().gpu_probe("GFX0")
        if dgpu_vendor:
            print(f"- Found GFX0: {dgpu_vendor}:{dgpu_device}")
            if dgpu_vendor == self.constants.pci_nvidia:
                if dgpu_device in PCIIDArray.nvidia_ids().tesla_ids or dgpu_device in PCIIDArray.nvidia_ids().fermi_ids:
                    if self.constants.detected_os > self.constants.catalina:
                        self.nvidia_legacy = True
                        self.amfi_must_disable = True
            elif dgpu_vendor == self.constants.pci_amd_ati:
                if dgpu_device in PCIIDArray.amd_ids().terascale_1_ids:
                    if self.constants.detected_os > self.constants.catalina:
                        self.amd_ts1 = True
                        self.amfi_must_disable = True
                # TODO: Enable TS2 support
                elif dgpu_device in PCIIDArray.amd_ids().terascale_2_ids:
                    if self.constants.detected_os > self.constants.catalina:
                        self.amd_ts2 = True
                        self.amfi_must_disable = True
        if igpu_vendor:
            print(f"- Found IGPU: {igpu_vendor}:{igpu_device}")
            if igpu_vendor == self.constants.pci_intel:
                if igpu_device in PCIIDArray.intel_ids().iron_ids:
                    if self.constants.detected_os > self.constants.catalina:
                        self.iron_gpu = True
                        self.amfi_must_disable = True
                elif igpu_device in PCIIDArray.intel_ids().sandy_ids:
                    if self.constants.detected_os > self.constants.catalina:
                        self.sandy_gpu = True
                        self.amfi_must_disable = True
                # TODO: Re-enable when Accel Patches are ready
                #elif igpu_device in PCIIDArray.intel_ids().ivy_ids:
                #    if self.constants.detected_os > self.constants.big_sur:
                #        self.ivy_gpu = True
            elif igpu_vendor == self.constants.pci_nvidia:
                if self.constants.detected_os > self.constants.catalina:
                    self.nvidia_legacy = True
                    self.amfi_must_disable = True

    def detect_patch_set(self):
        self.detect_gpus()
        if self.model in ModelArray.LegacyBrightness:
            if self.constants.detected_os > self.constants.catalina:
                self.brightness_legacy = True

        if self.model in ["iMac7,1", "iMac8,1"]:
            if self.constants.detected_os > self.constants.catalina:
                self.legacy_audio = True

        Utilities.cls()
        print("The following patches will be applied:")
        if self.nvidia_legacy is True:
            print("- Add Legacy Nvidia Tesla Graphics Patch")
        elif self.amd_ts1 is True:
            print("- Add Legacy ATI TeraScale 1 Graphics Patch")
        elif self.amd_ts2 is True:
            print("- Add Legacy ATI TeraScale 2 Graphics Patch")
        if self.iron_gpu is True:
            print("- Add Legacy Intel IronLake Graphics Patch")
        elif self.sandy_gpu is True:
            print("- Add Legacy Intel Sandy Bridge Graphics Patch")
        elif self.ivy_gpu is True:
            print("- Add Legacy Intel Ivy Bridge Graphics Patch")
        if self.brightness_legacy is True:
            print("- Add Legacy Brightness Control")
        if self.legacy_audio is True:
            print("- Add legacy Audio Control")

        if self.nvidia_legacy is False and \
            self.amd_ts1 is False and \
            self.amd_ts2 is False and \
            self.iron_gpu is False and \
            self.sandy_gpu is False and \
            self.ivy_gpu is False and \
            self.brightness_legacy is False and \
            self.legacy_audio is False:
            self.no_patch = True
        else:
            self.no_patch = False

    def verify_patch_allowed(self):
        self.sip_enabled, self.sbm_enabled, self.amfi_enabled, self.fv_enabled = Utilities.patching_status()
        if self.sip_enabled is True:
            print("\nCannot patch!!! Please disable SIP!!!")
            print("Disable SIP in Patcher Settings and Rebuild OpenCore")
            print("For Hackintoshes, set SIP to EF0F0000")
        if self.sbm_enabled is True:
            print("\nCannot patch!!! Please disable SecureBootModel!!!")
            print("Disable SecureBootModel in Patcher Settings and Rebuild OpenCore")
            print("For Hackintoshes, set SecureBootModel to Disabled")
        if self.fv_enabled is True:
            print("\nCannot patch!!! Please disable FileVault!!!")
            print("Go to System Preferences -> Security and disable FileVault")

        if self.amfi_enabled is True and self.amfi_must_disable is True:
            print("\nCannot patch!!! Please disable AMFI!!!")
            print("For Hackintoshes, please add amfi_getOut_of_my_way=0x1 to boot-args")

        if self.amfi_must_disable is True:
            if self.sip_enabled is True or \
            self.sbm_enabled is True or \
            self.amfi_enabled is True or \
            self.fv_enabled is True:
                return False
            else:
                return True
        else:
            if self.sip_enabled is True or \
            self.sbm_enabled is True or \
            self.fv_enabled is True:
                return False
            else:
                return True

    # Entry Function
    def start_patch(self):
        print("- Starting Patch Process")
        print(f"- Determinging Required Patch set for Darwin {self.constants.detected_os}")
        self.detect_patch_set()
        if self.no_patch is False:
            change_menu = input("Would you like to continue with Root Volume Patching?(y/n): ")
        else:
            change_menu = None
            print("- No Root Patches required for your machine!")
            input("\nPress [ENTER] to return to the main menu: ")
        if change_menu in ["y", "Y"]:
            print("- Continuing with Patching")
            print("- Verifying whether Root Patching possible")
            if self.verify_patch_allowed() is True:
                print("- Patcher is capable of patching")
                self.check_files()
                self.find_mount_root_vol(True)
            else:
                input("\nPress [ENTER] to return to the main menu: ")

        else:
            print("- Returning to main menu")

    def start_unpatch(self):
        print("- Starting Unpatch Process")
        if self.verify_patch_allowed() is True:
            self.find_mount_root_vol(False)
            input("\nPress [ENTER] to return to the main menu")

