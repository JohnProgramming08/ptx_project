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
import logging
import hashlib
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
debug = 0


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
    API_KEY = os.environ.get("API_KEY")
    URL_ROOT = os.environ.get("URL_ROOT")
    BIRD_BIN = os.environ.get("BIRD_BIN")

    # Following code should be fine on a typical Debian/Ubuntu system
    URL_LOCK = f"{URL_ROOT}/api/v4/router/get-update-lock"
    URL_CONF = f"{URL_ROOT}/api/v4/router/gen-config"
    URL_DONE = f"{URL_ROOT}/api/v4/router/updated"

    ETC_PATH = os.environ.get("ETC_PATH")
    RUN_PATH = os.environ.get("RUN_PATH")
    LOG_PATH = os.environ.get("LOG_PATH")
    LOCK_PATH = os.environ.get("LOCK_PATH")

    SLACK_URL = os.environ.get("SLACK_URL")

except Exception as e:
    logger.error("Environment variables not set correctly")
    error_exit(1, f"Environment variable error: {e}", "None set")


# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("-f", "--force", action="store_true", help="Force reload")
    parser.add_argument("-H", "--handle", type=str, help="Router handle")

    args = parser.parse_args()
    return args


# Create necessary directories for Bird
def create_directories():
    try:
        os.makedirs(ETC_PATH, exist_ok=True)
        os.makedirs(LOG_PATH, exist_ok=True)
        os.makedirs(RUN_PATH, exist_ok=True)
        os.makedirs(LOCK_PATH, exist_ok=True)
    except OSError as e:
        logger.error("Could not create directories most likely due to permissions")
        logger.error(f"Message - {e}")
        sys.exit()


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
    if debug:
        logger.debug(f"POST {URL_LOCK}/{handle} with API key {API_KEY}")

    try:
        response = requests.post(f"{URL_LOCK}/{handle}", headers=headers)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.error("ABORTING: router not available for update")
        logger.error(f"Message - {e}")
        sys.exit(200)


# Get the config from the IXP Manager
def get_config(handle, dest, headers):
    if debug:
        logger.debug(f"GET {URL_CONF}/{handle} with API key {API_KEY}")

    try:
        response = requests.get(f"{URL_CONF}/{handle}", headers=headers)
        response.raise_for_status()

        with open(dest, "w") as file:
            file.write(response.text)

    except requests.exceptions.HTTPError as e:
        logger.error(f"Non-zero return from curl when generating {dest}")
        logger.error(f"Message - {e}")
        sys.exit()

    except IOError as e:
        logger.error(f"Could not write to {dest}")
        logger.error(f"Message - {e}")
        sys.exit()


# Check if the generated file is valid
def is_valid_file(dest):
    if not os.path.exists(dest) or not os.path.getsize(dest):
        logger.error(f"{dest} does not exist or is zero size")
        sys.exit(3)

    with open(dest, "r") as file:
        contents = file.read()
        bgp_count = contents.count("protocol bgp pb_")
        if bgp_count < 2:
            logger.error(
                f"Fewer than 2 BGP protocol definitions in config file {dest} - something has gone wrong..."
            )
            sys.exit(4)


# Parse and check the config file
def parse_config(dest):
    command = f"{BIRD_BIN} -p -c {dest}"
    if debug:
        logger.debug(f"Checking config file {dest} for errors")
    try:
        subprocess.run(command, check=True, capture_output=True, shell=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Non-zero return from {BIRD_BIN} when parsing {dest}")
        logger.error(f"Message - {e}")
        sys.exit(7)


# Apply config and start Bird if neededj
# Filter out the comments from the given file
def filter_comments(file_path):
    filtered_file = ""
    with open(file_path, "r") as file:
        lines = file.readlines()
        for line in lines:
            if not line.strip()[0]:
                filtered_file += line
        return filtered_file


# USE HASH INSTEAD OF COMPARING LINES ITS QUICKER
def detect_change(cfile, dest):
    if os.path.exists(cfile):
        cfile_filtered = filter_comments(cfile)
        dest_filtered = filter_comments(dest)

        hash = hashlib.sha256()
        hash.update(cfile_filtered)
        cfile_hash = hash.hexdigest()
        hash.update(dest_filtered)
        dest_hash = hash.hexdigest()

        if cfile_hash == dest_hash:
            logger.info("No changes detected")
            os.remove(dest)
            return 0
        else:
            os.copyfile(cfile, f"{cfile}.old")
            os.rename(dest, cfile)
            return 1
    else:
        os.rename(dest, cfile)
        return 1


# Does the Bird daemon need to be started
# Couldn't find a pythonic way to do this, so using subprocess
def revert_config(dest, cfile, socket):
    if os.path.exists(f"{cfile}.old"):
        logger.info("Trying to revert to previous")
        os.move(f"{cfile}.conf", f"{dest}.failed")
        os.move(f"{cfile}.old", cfile)
        command = f"{BIRD_BIN}c -s {socket} configure"
        if debug:
            logger.debug(command)
        try:
            subprocess.run(command, check=True, shell=True)
            logger.info("Successfully reverted")
        except subprocess.CalledProcessError as e:
            logger.error("Revert failed due to subprocess error")
            logger.error(f"Message - {e}")
            sys.exit(6)
        except IOError as e:
            logger.error("Rever failed due to IO error")
            logger.error(f"Message - {e}")
            sys.exit(6)


def reload_if_needed(socket, cfile, reload_required, dest):
    command = f"{BIRD_BIN}c -s {socket} show status"
    result = subprocess.run(command, capture_output=True, shell=True)

    if debug:
        logger.debug(command)

    # Unsuccesful command run
    if result.returncode != 0:
        command = f"{BIRD_BIN} -c {cfile} -s {socket}"
        if debug:
            logger.debug(command)
        try:
            subprocess.run(command, check=True, shell=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Could not start {BIRD_BIN} daemon with command: {command}")
            logger.error(f"Message - {e}")
            sys.exit(5)

    # Successful command run
    elif reload_required:
        try:
            command = f"{BIRD_BIN}c -s {socket} configure"
            if debug:
                logger.info(command)
            subprocess.run(command, check=True, shell=True)

        # Try to revert to the previous config
        except subprocess.CalledProcessError as e:
            logger.error(f"Reconfigure failed for {dest}")
            logger.error(f"Message - {e}")
            revert_config(dest, cfile, socket, debug, logger)

    elif debug:
        logger.debug("Bird running and no reload required so skipping configure")


# Tell IXP manager that the router has been updated
def inform_ixp_manager(handle, headers):
    if debug:
        logger.debug(f"POST {URL_DONE}/{handle} with API key {API_KEY}")

    inform_success = False
    while not inform_success:
        try:
            response = requests.post(f"{URL_DONE}/{handle}", headers=headers)
            response.raise_for_status()
            inform_success = True
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"Could not inform IXP Manager of update for {handle}, retrying in 60 seconds"
            )
            logger.error(f"Message - {e}")
            time.sleep(60)


def main():
    global debug  # As debug is modified in the main function
    force_reload = 0

    # Check if the script is run with any arguments
    args = parse_args()
    if args.debug:
        debug = 1
        logging.basicConfig(level=logging.DEBUG)
    if args.force:
        force_reload = 1
    if args.handle:
        handle = args.handle
    else:
        logger.error("Handle is required")
        sys.exit(1)

    create_directories()
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
    if debug:
        logger.debug(f"Show memory usage of each instance of {BIRD_BIN}")

    reload_if_needed(socket, cfile, reload_required, dest)
    # Inform IXP Manager that the router has been updated and release the lock
    inform_ixp_manager(handle, headers)
    sys.exit(0)


if __name__ == "__main__":
    main()
