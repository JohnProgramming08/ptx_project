#!/usr/bin/env python3
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
import logging
import hashlib
import shutil
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# Exit with an error code and send the error to Slack
def error_exit(code, error, handle):
    logger.error(f"Message - {error}")
    host = os.gethostname()
    if SLACK_URL:
        requests.post(
            SLACK_URL,
            json={
                "text": f"host: {host}, handle: {handle}, error: {error}, code: {code}",
            },
        )

    sys.exit(code)


# You need to set specific IXP Manager installation details
# You should only need to edit the first 3 lines
try:
    API_KEY = os.environ["API_KEY"]
    URL_ROOT = os.environ["URL_ROOT"]
    BIRD_BIN = os.environ["BIRD_BIN"]

    # Following code should be fine on a typical Debian/Ubuntu system
    URL_LOCK = f"{URL_ROOT}/api/v4/router/get-update-lock"
    URL_CONF = f"{URL_ROOT}/api/v4/router/gen-config"
    URL_DONE = f"{URL_ROOT}/api/v4/router/updated"

    ETC_PATH = os.environ["ETC_PATH"]
    RUN_PATH = os.environ["RUN_PATH"]
    LOG_PATH = os.environ["LOG_PATH"]
    LOCK_PATH = os.environ["LOCK_PATH"]

    SLACK_URL = os.environ.get["SLACK_URL"]

except KeyError as e:
    logger.error("Environment variables not set correctly")
    error_exit(1, f"Environment variable error: {e}", "None set")


# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("-f", "--force", action="store_true", help="Force reload")
    parser.add_argument("-H", "--handle", type=str, help="Router handle", required=True)

    args = parser.parse_args()
    return args


# Create necessary directories for Bird
def create_directories(handle):
    try:
        os.makedirs(ETC_PATH, exist_ok=True)
        os.makedirs(LOG_PATH, exist_ok=True)
        os.makedirs(RUN_PATH, exist_ok=True)
        os.makedirs(LOCK_PATH, exist_ok=True)
    except OSError as e:
        logger.error("Could not create directories most likely due to permissions")
        error_exit(2, e, "None set")


# Only allow one instance of the script to run at a time - script locking
def remove_lock(lock):
    if os.path.exists(lock):
        os.remove(lock)


def create_lock(lock, handle):
    if os.path.exists(lock):
        logger.info(
            f"There is another instance running for {handle} and locked via {lock}, exiting"
        )
        sys.exit(1)
    else:
        process_id = os.getpid()
        with open(lock, "w") as file:
            file.write(str(process_id))
        atexit.register(remove_lock(lock))


# Get a lock from IXP Manager to update the router
def get_lock(handle, headers):
    logger.debug(f"POST {URL_LOCK}/{handle} with API key {API_KEY}")

    try:
        response = requests.post(f"{URL_LOCK}/{handle}", headers=headers)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.error("ABORTING: router not available for update")
        error_exit(200, e, handle)


# Get the config from the IXP Manager
def get_config(handle, dest, headers):
    logger.debug(f"GET {URL_CONF}/{handle} with API key {API_KEY}")

    try:
        response = requests.get(f"{URL_CONF}/{handle}", headers=headers)
        response.raise_for_status()

        with open(dest, "w") as file:
            file.write(response.text)

    except requests.exceptions.HTTPError as e:
        logger.error(f"Non-zero return from curl when generating {dest}")
        error_exit(2, e, handle)

    except IOError as e:
        logger.error(f"Could not write to {dest}")
        error_exit(3, e, handle)


# Check if the generated file is valid
def is_valid_file(dest, handle):
    if not os.path.exists(dest) or not os.path.getsize(dest):
        logger.error(f"{dest} does not exist or is zero size")
        error_exit(3, f"File {dest} is invalid", handle)

    with open(dest, "r") as file:
        contents = file.read()
        bgp_count = contents.count("protocol bgp pb_")
        if bgp_count < 2:
            logger.error(
                f"Fewer than 2 BGP protocol definitions in config file {dest} - something has gone wrong..."
            )
            error_exit(4, "Less than 2 'protocol bgp pb_' definitions found", handle)


# Parse and check the config file
def parse_config(dest, handle):
    command = f"{BIRD_BIN} -p -c {dest}"
    logger.debug(f"Checking config file {dest} for errors")
    try:
        subprocess.run(command, check=True, capture_output=True, shell=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Non-zero return from {BIRD_BIN} when parsing {dest}")
        error_exit(7, e, handle)


# Apply config and start Bird if needed
# Filter out the comments from the given file
def filter_comments(file_path):
    filtered_file = ""
    with open(file_path, "r") as file:
        lines = file.readlines()
        for line in lines:
            if not line.strip().startswith("#"):
                filtered_file += line
        return filtered_file


# USE HASH INSTEAD OF COMPARING LINES ITS QUICKER
def detect_change(cfile, dest):
    if os.path.exists(cfile):
        cfile_filtered = filter_comments(cfile)
        dest_filtered = filter_comments(dest)

        cfile_hash = hashlib.sha256(cfile_filtered.encode("utf-8")).hexdigest()
        dest_hash = hashlib.sha256(dest_filtered.encode("utf-8")).hexdigest()

        if cfile_hash == dest_hash:
            logger.info("No changes detected")
            os.remove(dest)
            return 0
        else:
            shutil.copyfile(cfile, f"{cfile}.old")
            os.rename(dest, cfile)
            return 1
    else:
        os.rename(dest, cfile)
        return 1


# Does the Bird daemon need to be started
# Couldn't find a pythonic way to do this, so using subprocess
def revert_config(dest, cfile, socket, handle):
    if os.path.exists(f"{cfile}.old"):
        logger.info("Trying to revert to previous")
        shutil.move(f"{cfile}.conf", f"{dest}.failed")
        shutil.move(f"{cfile}.old", cfile)
        command = f"{BIRD_BIN}c -s {socket} configure"
        logger.debug(command)
        try:
            subprocess.run(command, check=True, shell=True)
            logger.info("Successfully reverted")
        except subprocess.CalledProcessError as e:
            logger.error("Revert failed due to subprocess error")
            error_exit(6, e, handle)
        except IOError as e:
            logger.error("Rever failed due to IO error")
            error_exit(6, e, handle)


def reload_if_needed(socket, cfile, reload_required, dest, handle):
    command = f"{BIRD_BIN}c -s {socket} show status"
    result = subprocess.run(command, capture_output=True, shell=True)

    logger.debug(command)

    # Unsuccesful command run
    if result.returncode != 0:
        command = f"{BIRD_BIN} -c {cfile} -s {socket}"
        logger.debug(command)
        try:
            subprocess.run(command, check=True, shell=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Could not start {BIRD_BIN} daemon with command: {command}")
            error_exit(5, e, handle)

    # Successful command run
    elif reload_required:
        try:
            command = f"{BIRD_BIN}c -s {socket} configure"
            logger.info(command)
            subprocess.run(command, check=True, shell=True)

        # Try to revert to the previous config
        except subprocess.CalledProcessError as e:
            revert_config(dest, cfile, socket)
            logger.error(f"Reconfigure failed for {dest}")
            error_exit(6, e, handle)

    logger.debug("Bird running and no reload required so skipping configure")


# Tell IXP manager that the router has been updated
def inform_ixp_manager(handle, headers):
    logger.debug(f"POST {URL_LOCK}/{handle} with API key {API_KEY}")

    inform_success = False
    while not inform_success:
        try:
            response = requests.post(f"{URL_DONE}/{handle}", headers=headers)
            response.raise_for_status()
            inform_success = True
        except requests.exceptions.HTTPError as e:
            logger.warning(
                f"Could not inform IXP Manager of update for {handle}, retrying in 60 seconds"
            )
            logger.error(f"HTTP error: {e}")
            time.sleep(60)


def main():
    force_reload = 0

    # Check if the script is run with any arguments
    args = parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    if args.force:
        force_reload = 1
    handle = args.handle

    create_directories(handle)
    cfile = f"{ETC_PATH}/bird-{handle}.conf"
    dest = f"{cfile}.$$"
    socket = f"{RUN_PATH}/bird-{handle}.ctl"

    # Prevent multiple instances of the script running at the same time
    lock = f"{LOCK_PATH}/{handle}.lock"
    create_lock(lock, handle)

    headers = {"X-IXP-Manager-API-Key": API_KEY}
    get_lock(handle, headers)

    get_config(handle, dest, headers)
    is_valid_file(dest)
    parse_config(dest)

    # Config file is valid if this point is reached
    reload_required = detect_change(cfile, dest)
    if force_reload:
        reload_required = 1
    logger.debug(f"Show memory usage of each instance of {BIRD_BIN}")

    reload_if_needed(socket, cfile, reload_required, dest)
    # Inform IXP Manager that the router has been updated and release the lock
    inform_ixp_manager(handle, headers)
    sys.exit(0)


if __name__ == "__main__":
    main()
