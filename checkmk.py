import requests
from extras.scripts import *
from dcim.models import Device

"""
> CheckMK Device Sync for Netbox v3.x
> Written by Maximilian Thoma 2022
> Version 1.0
########################################################################################################################
Quick Manual:
-------------
1. Place the file in /etc/netbox/scripts folder or in case of docker in ./scripts in the docker root of netbox.
!!! For automatic operation: You need a user with token + permission to run scripts.

2. Create webhook

Method: POST
URL: http://<netbox>/api/extras/scripts/checkmk.CheckMKDeviceSync/

Additional Headers:
Authorization: Token <token>

Body Template:
{"data": {"event": "{{ event }}", "name": "{{ data.name }}", "data": {{ data|tojson }}}, "commit": true}

HTTP Content Type: application/json
Events: Update, Delete
Object types: dcim | device

3. Modify parameters for checkmk below 

Note: Create event is not required because device must have an IP before we can create it in CheckMK.
"""

# VARS for CheckMK
########################################################################################################################
# Hostname of CheckMK host
HOST_NAME="127.0.0.1:8080"
# Sitename
SITE_NAME="cmk"
# URL generator (only change here something if you use SSL for example)
API_URL=f"http://{HOST_NAME}/{SITE_NAME}/check_mk/api/1.0"
# Automation user
USERNAME="automation"
# Automation secret
PASSWORD="secret"
# Basepath in folder structure you can also use / as root.
ROOT_FOLDER = "/network"
# Label for devices created by netbox
# TODO: Add label to host 
TAG = "managedby:netbox"

# Trigger scan & commit change
# TODO: Not implemented yet
SCAN = True
COMMIT = True

########################################################################################################################
# DO NOT CHANGE SOMETHING BELOW !!!
########################################################################################################################

# Init Session
########################################################################################################################
session = requests.session()
session.headers['Authorization'] = f"Bearer {USERNAME} {PASSWORD}"
session.headers['Accept'] = 'application/json'
session.verify = False

# Shared functions
########################################################################################################################
def create_folder(parent, folder):
    """
    Create a new folder in check_mk tree
    """
    resp = session.post(
        f"{API_URL}/domain-types/folder_config/collections/all",
        headers={
            "Content-Type": 'application/json',
            # (required) A header specifying which type of content is in the request/response body.
        },
        json={
            'name': folder,
            'title': folder,
            'parent': parent,
        },
    )
    if resp.status_code == 200:
        return True


def check_folder(folder_path):
    """
    Check if folder path exists in check_mk
    """
    # Check MK needs remplacement for / because it can't be part of url
    folder_path = folder_path.replace("/", "~")
    resp = session.get(
        f"{API_URL}/objects/folder_config/{folder_path}",
        params={
            "show_hosts": False,
        },
    )
    if resp.status_code == 200:
        return True
    else:
        return False


def check_folders(path):
    """
    Check complete folder path
    """
    parent = "/"
    px = path.split("/")
    for p in px[1:]:
        target = parent + f"{p}/"

        if check_folder(target[:-1]) is not True:
            create_folder(parent, p)
        else:
            pass
        parent = target


def check_host_exists(hostname):
    """
    Check if hosts exists in checkmk and gives object back
    """
    resp = session.get(
        f"{API_URL}/objects/host_config/{hostname}",
        params={
            "effective_attributes": False,
        },
    )
    if resp.status_code == 200:
        return True, resp.json(), resp.headers.get('ETag','')
    else:
        return False, {}, ""


def create_host(hostname, xpath, ip):
    resp = session.post(
        f"{API_URL}/domain-types/host_config/collections/all",
        params={  # goes into query string
            "bake_agent": False,  # Tries to bake the agents for the just created hosts.
        },
        headers={
            "Content-Type": 'application/json',
            # (required) A header specifying which type of content is in the request/response body.
        },
        json={
            'folder': xpath,
            'host_name': hostname,
            'attributes': {
                'ipaddress': ip
            }
        },
    )
    if resp.status_code == 200:
        return True, resp.json()
    else:
        return False, {"code": resp.status_code}


def delete_host(hostname):
    """
    Delete host from CheckMK
    """
    resp = session.delete(
        f"{API_URL}/objects/host_config/{hostname}",
    )

    if resp.status_code == 204:
        return True, resp.status_code
    else:
        return False, resp.status_code


def update_ip_of_host(hostname, ip, etag):
    """
    Update ip of host
    """
    resp = session.put(
        f"{API_URL}/objects/host_config/{hostname}",
        headers={
            "If-Match": etag,
            "Content-Type": 'application/json',

        },
        json={
            'update_attributes': {
                'ipaddress': ip
            },
        },
    )
    if resp.status_code == 200:
        return True, resp.status_code
    else:
        return False, resp.status_code


def move_to_folder(hostname, folder, etag):
    """
    Move host to new folder
    """
    xfolder = folder.replace("/", "~")
    resp = session.post(
        f"{API_URL}/objects/host_config/{hostname}/actions/move/invoke",
        headers={
            "If-Match": etag,
            "Content-Type": 'application/json',

        },
        json={'target_folder': xfolder},
    )
    if resp.status_code == 200:
        return True, resp.status_code
    else:
        return False, resp.status_code




# Netbox Script
########################################################################################################################
class CheckMKDeviceSync(Script):
    class Meta:
        name = "CheckMK Device Sync for Netbox"
        description = "Script create, update & delete objects in CheckMK"

    def device_update(self, data):
        """
        Create / Update device
        """
        self.log_debug("Device update triggered ...")

        name = data['name']

        d = Device.objects.get(id=data['data']['id'])

        ip = str(d.primary_ip4.address.ip)
        site = d.site.slug

        if d.site.region is not None:
            region = d.site.region.slug
            if d.site.region.parent is not None:
                parent_region = d.site.region.parent.slug
            else:
                parent_region = ""
        else:
            region = ""
            parent_region = ""

        # Construct path
        if parent_region != "":
            xpath = f"{ROOT_FOLDER}/{parent_region}/{region}/{site}"
        elif region != "":
            xpath = f"{ROOT_FOLDER}/{region}/{site}"
        else:
            xpath = f"{ROOT_FOLDER}/{site}"

        # 1. Check if all folders are present
        check_folders(xpath)

        hstate, hdata, etag = check_host_exists(name)

        # 2. Check if device exist or not
        if hstate is True:
            self.log_info(f"{name}: host exist in checkmk")
            hfolder = hdata['extensions']['folder']
            hip = hdata['extensions']['attributes']['ipaddress']

            if hfolder != xpath:
                self.log_warning(f"{name}: Host in wrong folder")
                fstate, fcode = move_to_folder(name, xpath, etag)
                if fstate is True:
                    self.log_success(f"{name}: Host moved from {hfolder} to {xpath}")
                else:
                    self.log_failure(f"{name}: Host move not successful, code: {fcode}")

            if hip != ip:
                self.log_warning(f"{name}: IP address not equal")
                ustate, ucode = update_ip_of_host(name, ip, etag)
                if ustate is True:
                    self.log_success(f"{name}: Host IP updated from {hip} to {ip}")
                else:
                    self.log_failure(f"{name}: Host IP update failed code:{ucode}")
        else:
            self.log_info(f"{name}: Device not existing create it ...")
            cstate, cdata = create_host(name, xpath, ip)
            if cstate is True:
                self.log_success(f"{name}: host created in checkmk")
            else:
                self.log_warning(f"{name}: error occured while creating host details: {cdata}")


    def device_deleted(self, data):
        """
        Delete device
        """
        self.log_debug("Device delete triggered ...")
        state, code = delete_host(data['name'])
        if state is True:
            self.log_success(f"{data['name']}: Device deleted in CheckMK")
            return {"state": "successful", "code": code}
        else:
            self.log_failure(f"{data['name']}: Device delete in CheckMK was not successful.")
            return {"state": "failed", "code": code}


    def run(self, data, commit):
        if "event" not in data.keys():
            return {"message": "This script can only be triggered by webhook."}

        if data['event'] == "updated":
            return self.device_update(data)
        elif data['event'] == "deleted":
            return self.device_deleted(data)
        else:
            self.log_failure("Unknown action triggered")
            return {"message": "Unknown action triggered"}
