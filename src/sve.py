#!/usr/bin/env python3

"""
SNX VPN Emulator.

This CLI tool can manipulate virtual vpn connections
based on CheckPoint's SNX technology:

(https://support.checkpoint.com/results/sk/sk65210)

It's using QEMU emulation platform and ubuntu-based image.
For token workflow need to have stoken cli tool installed.
"""

import argparse
import configparser
import os
import re
import shlex
import shutil
import subprocess
import sys
from getpass import getpass
from time import sleep

import pexpect


COLOR_GRN = "\033[1;32m"
COLOR_YLW = "\033[1;33m"
COLOR_END = "\033[0m"
MSG_TMPLT = ":: {}{}{}"

DEF_SVE_INI = {
                "otp_tool": "stoken",
                "otp_pin": "",
                "base_img": ""
              }
DEF_VM_INI = {
               "vm_system": "qemu-system-x86_64",
               "vm_mem": "512",
               "vm_user": "",
               "vm_pwd": ""
             }
DEF_VPN_INI = {
               "example1": "<localhost_port>;<snx_connection>",
               "example2": "2201;snx -s foo.com -u bar"
              }

VM_CMD = ("{system}"
          " -m {mem}"
          " -nographic"
          " -drive file={file},if=virtio"
          " {hw_accel}"
          " -cpu host"
          " -nic user,hostfwd=tcp::{loc_port}-:22")

VM_EXPECT = \
"""#!{exp_path}

spawn $env(SHELL)

send "ssh -p {port} -o StrictHostKeyChecking=no \
-o UserKnownHostsFile=/dev/null {user}@127.0.0.1\r"
expect -exact "password"
send "{pwd}\r"
expect -exact "vpn-snx"
send "{snx_conn}\r"
expect {{
  -exact "password" {{
    send "{otp}\r"

    expect {{
      -exact "accept" {{
        send "y"
      }}
      -exact "vpn-snx" {{
      }}
    }}

  }}
  -exact "aborting..." {{
    send "uptime\r"
  }}
}}
interact
"""


class SveManager:
    """SVE (Virtual VPN connections) manager."""

    def __init__(self):
        """Set default params and settings."""
        self.HOME = os.path.expanduser("~/.sve/")
        self.CONF = self.HOME + "conf.ini"
        self.platform = sys.platform
        self._conn_retry_count = 0

        self._define_userenv_params()

        self._parse_config()
        self._check_config()

    def _parse_config(self):
        """
        Use .ini config file to populate class with settings.

        In case of config is missing - generate default one.
        """
        # read config if available
        if os.path.isfile(self.CONF):
            conf = configparser.ConfigParser()
            conf.read(self.CONF)

            self.otp_tool = conf["SVE"]["otp_tool"]
            self.PIN = conf["SVE"]["otp_pin"]
            self.bimg_path = os.path.expanduser(conf["SVE"]["base_img"])
            self.bimg_name = os.path.basename(self.bimg_path)

            self.user = conf["VM"]["vm_user"]
            self.pwd = conf["VM"]["vm_pwd"]
            self.vm_emu_sys = conf["VM"]["vm_system"]
            self.vm_mem = conf["VM"]["vm_mem"]

            self.connections = conf["VPN"].keys()

            self.conf = conf

        # if not create from template
        else:
            conf = configparser.ConfigParser()

            conf["SVE"] = DEF_SVE_INI
            conf["VM"] = DEF_VM_INI
            conf["VPN"] = DEF_VPN_INI

            # home dir and config
            if not os.path.isdir(self.HOME):
                os.mkdir(self.HOME)
            with open(self.CONF, "w") as cf:
                conf.write(cf)

            # re-read new config
            self._parse_config()
            echo_msg("Default config file was created")

    def _check_config(self):
        """
        Check essential settings.

        Some of them are important, no reason for
        the tool to be invoked without them.
        """
        msg = list()
        if not self.otp_tool:
            msg.append("OTP tool is not set")
        if not self.bimg_path:
            msg.append("Image file is not set")
        if not self.vm_emu_sys:
            msg.append("VM exec/system is not set")
        if not self.vm_mem:
            msg.append("VM RAM is not set")
        if not self.user:
            msg.append("VM user is not set")
        if not self.pwd:
            msg.append("VM pass is not set")

        if msg:
            summary = "Please edit config file: {}".format(self.CONF)
            msg.append(summary)

            echo_msg(msg)
            sys.exit(1)

    def _cmd_present(self, cmdtool, return_path=False):
        """Define if cli tool is installed."""
        # return:
        # str -> info message
        # bool -> tool exists
        # path -> full path to cli tool
        path = shutil.which(cmdtool)

        if return_path:
            return path

        return "Looks like '{}' not installed or" \
               " not in $PATH".format(cmdtool) if not path else True

    def _define_userenv_params(self):
        """Get OS/hardware based parameter."""
        params = dict()
        if self.platform == "darwin":
            params["accel"] = "-accel hvf"
            params["file_ext"] = "command"
            params["exec_cmd"] = "os.system('open {}')"
        # other OS
        else:
            params["accel"] = ""
            params["file_ext"] = "sh"
            params["exec_cmd"] = "To connect exec {} in your" \
                                 " favorite terminal emulator"

        self.env_params = params

    def _check_port_avail(self, port=22):
        """
        Check layer 3 TCP port for sshd.

        Should be open and able to accept connection.
        """
        check_cmd = self._cmd_present("ssh")
        if isinstance(check_cmd, bool):

            stat = "success"
            sh_cmd = "ssh -o StrictHostKeyChecking=no" \
                     " -o UserKnownHostsFile=/dev/null" \
                     " -p {} {}@127.0.0.1".format(port, self.user)
            try:
                pexcp = pexpect.spawn(sh_cmd)
                pexcp.expect('password')
            except (pexpect.exceptions.EOF,
                    pexpect.exceptions.TIMEOUT):
                stat = "failed"
                self._conn_retry_count += 1

            return stat

        # no ssh tool found, exit
        else:
            echo_msg(check_cmd)
            sys.exit(1)

    def _run_connect_emulator(self, port, snx_conn_str):
        """Check, run, interact with SNX VM/emulator."""
        # check for base image
        if os.path.isfile(self.bimg_path):

            # check for target image in app's home
            timg_name = self.bimg_name.replace("XXXX", port)
            timg_path = self.HOME + timg_name

            # copy base img if no target found
            if not os.path.isfile(timg_path):
                echo_msg("Copying {} to {}...".format(self.bimg_path,
                                                      timg_path))
                shutil.copyfile(self.bimg_path, timg_path)
                echo_msg("... done")

            # start/check VM

            # if VM is running QEMU will raise port mapping exception
            # since port already in use,
            # silence this by redirecting stdout/stderr if no -d specified
            cmd = VM_CMD.format(system=self.vm_emu_sys, mem=self.vm_mem,
                                file=timg_path,
                                hw_accel=self.env_params["accel"],
                                loc_port=port)
            sh_cmd = shlex.split(cmd)

            if not argv.debug:
                vm_process = subprocess.Popen(sh_cmd,
                                              stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
            else:
                vm_process = subprocess.Popen(sh_cmd)

            # subprocess should be alive while VM is running
            # check connection for 3 times
            while all([vm_process.poll() is None,
                       self._conn_retry_count < 3]):

                stat = self._check_port_avail(port)
                # port is up and sshd responding
                if stat == "success":
                    echo_msg("Connecting to VM")
                    self._connect_to_vm(port, snx_conn_str)
                    break
                else:
                    # something wrong, need to check VM
                    if self._conn_retry_count == 3:
                        echo_msg("No connection to VM, try to use"
                                 " -d or check VM manually")
                    else:
                        echo_msg("Waiting for port to be up")
                        sleep(5)

        # no base img found
        else:
            echo_msg("Maybe no base image?")

    def _connect_to_vm(self, port, snx_conn_str):
        """Connect to running emulator."""
        otp_request = self.get_otp()
        # eliminate error message
        otp_match = re.match(r"^\d*$", otp_request)
        if otp_match:
            otp = otp_match.group()

            # save connection script and run it
            exp_path = self._cmd_present("expect", return_path=True)
            if exp_path:

                script = VM_EXPECT.format(exp_path=exp_path,
                                          port=port,
                                          user=self.user,
                                          pwd=self.pwd,
                                          snx_conn=snx_conn_str,
                                          otp=otp)

                path = "{}snx_{}.{}".format(self.HOME,
                                            port,
                                            self.env_params["file_ext"])

                # write it and make executable
                with open(path, "w") as file:
                    file.write(script)
                    subprocess.call(["chmod", "+x", path])

                # execute script
                command = self.env_params["exec_cmd"].format(path)
                eval(command)

            # no expect tool found
            else:
                echo_msg("No 'expect' cli tool found")

        # otp related error occurred
        else:
            echo_msg(otp_request)

    def run(self):
        """Run VPN VM."""
        check_cmd = self._cmd_present(self.conf["VM"]["vm_system"])
        if isinstance(check_cmd, bool):

            vpn_name = argv.connect[0]

            # avoid to use "example" record
            if "example" in vpn_name:
                echo_msg("Can't use example record")
                sys.exit(0)

            # get connection parameters
            if vpn_name in self.connections:
                conn_info = self.conf["VPN"][vpn_name]
                port, conn_str = conn_info.split(";")

                self._run_connect_emulator(port, conn_str)

            else:
                echo_msg("Maybe wrong VPN name?")

        # no qemu system binary found
        else:
            echo_msg(check_cmd)

    def get_otp(self):
        """Get One Time Passcode."""
        check_cmd = self._cmd_present(self.otp_tool)
        if isinstance(check_cmd, bool):

            # PIN could be not set in config for security reason
            # need to ask it and use in current cli session only
            if not self.PIN:
                msg = COLOR_YLW + ":: Enter secure PIN: " + COLOR_END
                self.PIN = getpass(prompt=msg)

            # logic for stoken cli tool,
            # if using another one - maybe need to adjust
            # pexpect interaction
            try:
                shell = pexpect.spawn(self.otp_tool, maxread=100)

                shell.expect(["Enter password to decrypt token:"], timeout=5)
                shell.sendline(self.PIN)
                shell.expect(["Enter PIN:"], timeout=5)
                shell.sendline(self.PIN)

                shell.expect(r'\d+', timeout=5)
                otp = shell.after.decode("utf-8")

                shell.close()
            except Exception as err:

                if isinstance(err, pexpect.exceptions.TIMEOUT):
                    otp = "Error getting OTP, check your PIN"
                # or any other exception can be added later
                else:
                    otp = "Unknown error occurred"

            return otp

        # no token cli tool found
        else:
            return check_cmd

    def get_connections(self):
        """Get available VPNs from config."""
        return self.connections


def echo_msg(msg):
    """Print formatted message to stdout."""
    if not argv.silence:
        # if one msg/not iterable - convert to list
        # to support "for" loop (multiple msgs)
        if isinstance(msg, str):
            msg = [msg]

        for item in msg:
            print(MSG_TMPLT.format(COLOR_GRN,
                                   item,
                                   COLOR_END))


def usage():
    """Parse cli arguments."""
    parser = argparse.ArgumentParser()

    ex_group = parser.add_mutually_exclusive_group()

    ex_group.add_argument("-p", "--otp",
                          help="get One Time Pass code"
                               " (default option if no other specified)",
                          action="store_true")
    ex_group.add_argument("-gc", "--get-conf",
                          help="get config file path",
                          action="store_true")
    ex_group.add_argument("-l", "--list",
                          help="get configured VPNs",
                          action="store_true")
    ex_group.add_argument("-c", "--connect",
                          metavar="[VPN]",
                          help="connect to given VPN",
                          action="store",
                          nargs=1,
                          type=str)
    parser.add_argument("-s", "--silence",
                        help="silence output (only PIN prompt available)",
                        action="store_true")
    parser.add_argument("-d", "--debug",
                        help="show QEMU output",
                        action="store_true")

    args = parser.parse_args()

    # show help if no args received.
    # if len(sys.argv) == 1:
    #     parser.print_help()
    #     sys.exit(0)

    return args


def main():
    """Invoke logic according to arguments."""
    if argv.get_conf:
        echo_msg(svem.CONF)
    elif argv.list:
        echo_msg(svem.get_connections())
    elif argv.connect:
        svem.run()
    # default is to get OTP
    else:
        echo_msg(svem.get_otp())


if __name__ == "__main__":
    """Script's main entrypoint."""
    argv = usage()
    svem = SveManager()

    main()
