#! /bin/bash
#
# Copyright (C) 2009 - 2024 Internet Neutral Exchange Association Company Limited By Guarantee.
# All Rights Reserved.
#
# This file is part of IXP Manager.
#
# IXP Manager is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, version 2.0 of the License.
#
# IXP Manager is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License v2.0
# along with IXP Manager.  If not, see:
#
# http://www.gnu.org/licenses/gpl-2.0.html
#
# Checks if a config file has changed by comparing the whole file before and after
import sys
import os
import argparse
import atexit
import requests
import subprocess
import time

# You need to set specific IXP Manager installation details
# You should only need to edit the first 3 lines

api_key = "your-api-key"
url_root = "https://ixp.example.com"
bird_bin = "/usr/sbin/bird"

# Following code should be fine on a typical Debian/Ubuntu system
url_lock = f"{url_root}/api/v4/router/get-update-lock"
url_conf = f"{url_root}/api/v4/router/gen-config"
url_done = f"{url_root}/api/v4/router/updated"

etc_path = "/usr/local/etc/bird"
run_path = "/var/run/bird"
log_path = "/var/log/bird"
lock_path = "/tmp/ixp-manager-locks"


# Parse command line arguments and set necessary variables

# Set as normal variables for now but should be changed to env variables later
debug = 0
force_reload = 0

# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("-f", "--force", action="store_true", help="Force reload")
    parser.add_argument("-H", "--handle", type=str, help="Router handle")
    
    args = parser.parse_args()
    return args

# Check if the script is run with any arguments
args = parse_args()
if args.debug:
	debug = 1
if args.force:
    force_reload = 1
if args.handle:
	handle = args.handle
else:
    print("ERROR: handle is required")
    sys.exit(1)

# Create necessary directories for Bird
try:
	os.makedirs(etc_path, exist_ok=True)
	os.makedirs(log_path, exist_ok=True)
	os.makedirs(run_path, exist_ok=True)
	os.makedirs(lock_path, exist_ok=True)
except:
    print("ERROR: could not create directories most likely due to permissions")
    sys.exit()

cfile = f"{etc_path}/bird-{handle}.conf"
dest = f"{cfile}.$$"
socket = f"{run_path}/bird-{handle}.ctl"

# Only allow one instance of the script to run at a time - script locking
lock = f"{lock_path}/{handle}.lock"

def remove_lock():
    if os.path.exists(lock):
        os.remove(lock)

def create_lock():
    if os.path.exists(lock):
        print(f"There is another instance running for {handle} and locked via {lock}, exiting")
        sys.exit(1)
    else:
        process_id = os.getpid()
        with open(lock, "w") as file:
            file.write(str(process_id))
        atexit.register(remove_lock)
        
create_lock()

# Get a lock from IXP Manager to update the router
headers = {"X-IXP-Manager-API-Key": api_key}

if debug:
    print(f"POST {url_lock}/{handle} with API key {api_key}")
    
try:
    response = requests.post(f"{url_lock}/{handle}", headers=headers)
    response.raise_for_status()
except:
    print("ABORTING: router not available for update")
    sys.exit(200)
    
# Get the config from the IXP Manager
if debug:
	print(f"GET {url_conf}/{handle} with API key {api_key}")
    
try:
    response = requests.get(f"{url_conf}/{handle}", headers=headers)
    response.raise_for_status()
    
    with open(dest, "w") as file:
        file.write(response.text)

except:
    print(f"ERROR: non-zero return from curl when generating {dest}")
    sys.exit()

# Check if the generated file is valid
if not os.path.exists(dest) or not os.path.getsize(dest):
    print(f"ERROR: {dest} does not exist or is zero size")
    sys.exit(3)
    
with open(dest, "r") as file:
    contents = file.read()
    bgp_count = contents.count("protocol bgp pb_")
    if bgp_count < 2:
        print(f"ERROR: fewer than 2 BGP protocol definitions in config file {dest} - something has gone wrong...")
        sys.exit(4)

# Parse and check the config file
def parse_config():
    command = f"{bird_bin} -p -c {dest}"
    if debug:
        print(f"Checking config file {dest} for errors")
    try:
        result = subprocess.run(command, check=True, capture_output=True, shell=True)
    except:
        print(f"ERROR: non-zero return from {bird_bin} when parsing {dest}")
        sys.exit(7)

parse_config()
# Config file is valid if this point is reached

# Apply config and start Bird if needed

reload_required = 1

# Filter out the comments from the given file
def filter_comments(file_path):
    filtered_lines = []
    with open(file_path, "r") as file:
        lines = file.readlines()
        for line in lines:
            if not line.strip()[0]:
                filtered_lines.append(line)
        return filtered_lines
    
if os.path.exists(cfile):
    cfile_filtered = filter_comments(cfile)
    dest_filtered = filter_comments(dest)
    if cfile_filtered == dest_filtered:
        print("No changes detected")
        reload_required = 0
        os.remove(dest)
    else:
        os.copyfile(cfile, f"{cfile}.old")
        os.rename(dest, cfile)
else:
    os.rename(dest, cfile)
    
if force_reload == 1:
    reload_required = 1

# Does the Bird daemon need to be started
if debug:
    print(f"Show memory usage of each instance of {bird_bin}")


# Couldn't find a pythonic way to do this, so using subprocess


command = f"{bird_bin}c -s {socket} show status"
result = subprocess.run(command, capture_output=True, shell=True)
    
if debug:
	print(command)

# Unsuccsful command run
if result.returncode != 0:
	command = f"{bird_bin} -c {cfile} -s {socket}"
	if debug:
		print(command)
	try:
		subprocess.run(command, check=True, shell=True)
	except:
		print(f"ERROR: could not start {bird_bin} daemon with command: {command}")
		sys.exit(5)

# Successful command run
elif reload_required:
    try:
        command = f"{bird_bin}c -s {socket} configure"
        if debug:
            print(command)
        subprocess.run(command, check=True, shell=True)
    
	# Try to revert to the previous config
    except:
        print(f"ERROR: Reconfigure failed for {dest}")
        if os.path.exists(f"{cfile}.old"):
            print("Trying to revert to previous")
            os.move(f"{cfile}.conf", f"{dest}.failed")
            os.move(f"{cfile}.old", cfile)
            command = f"{bird_bin}c -s {socket} configure"
            if debug:
                print(command)
            try:
                subprocess.run(command, check=True, shell=True)
                print("Successfully reverted")
            except:
                print("ERROR: Revert failed")
                sys.exit(6)

elif debug:
    print("Bird running and no reload required so skipping configure")    


# Tell IXP manager that the router has been updated
if debug:
    print(f"POST {url_done}/{handle} with API key {api_key}")

inform_success = False
while not inform_success:
    try:
        response = requests.post(f"{url_done}/{handle}", headers=headers)
        response.raise_for_status()
        inform_success = True
    except:
        print(f"Warning - could not inform IXP Manager of update for {handle}, retrying in 60 seconds")
        time.sleep(60)

sys.exit(0)

