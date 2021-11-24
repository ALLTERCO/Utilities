######################################################################################################################
#
#  IoT Device Provisioning Script
#  Author: Kerry Clendinning
#  Copyright (c) 2021 Allterco Robotics US
#  Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
#  in compliance with the License. You may obtain a copy of the License at:
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
#  on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
#  for the specific language governing permissions and limitations under the License.
#
#  Shelly is the Trademark and Intellectual Property of Allterco Robotics Ltd.
#
######################################################################################################################
#
#  tl;dr: Run the program with "features" or "help" to learn more
#
#  ex: python automagic.py features
#
######################################################################################################################
#
#  Changes:
#
# 1.0010     Now caches OTA build info
#            
# 1.0009     DD-WRT works with spaces and "&" in SSID
#
# 1.0008     Completed config-test feature
#
# 1.0007     Attributes now stored under ConfigInput and ConfigStatus JSON objects instead of top-level.
#            Checks Shelly firmware version (for LATEST) by querying https://api.shelly.cloud/files/firmware
#            Added acceptance-test feature (working) and config-test (work-in-progress)
#
# 1.0006     Now provision-list can work without dd-wrt devices, but only to program IoT devices onto the same
#            WiFi network as the computer/laptop.  This allows other fields like Static IP and device name to be
#            set during initial provisioning.  The provision-list operation is also compatible with --settings now.
#
# 1.0005     Added --access ALL|Periodic|Continuous features for probe-list and apply to work with
#            battery-powered devices with periodic WiFi access
#
# 1.0004     apply operation now updates settings in db
#            apply now takes --settings argument
#
# 1.0003   - filter some settings from the copy operation during a replace, including "fw" which isn't settable
#            added LatLng and TZ to import and provision-list
#            added --settings to support Lat/Lng with simple provision operation
#            improved error handling of timeouts during factory-reset
#
# 1.0002   - fixed IP address by re-getting status in provision_ddwrt()
#            fixed some python3 compatibility issues
#            re-fetch settings after device name changes in provision_native()/provision_ddwrt()
#            added identify operation
#            added --restore-device option to apply
#            added Gateway to import column options
#            better query columns expansion e.g. wifi_sta.gw will now match settings.wifi_sta.gw
#            added replace operation
#
######################################################################################################################
#
#  TODO:
#            Support devices with access control PIN code
#
#            Actions for motion sensor to support interval:
#                  192.168.56.2/settings/actions?index=0&enabled=true&name=motion_on&urls[0][url]=http%3A%2F%2Fwhat.com&urls[0][int]=0000-0000
#
#            Test other special characters beyond "&": *,+'"`!#%() etc.  Test w/DD-WRT, Windows, Mac
#
#            Remove depenency on requests module for python3.8/dd-wrt functionality (?)
#
#            Enforce SSID/password limitations
#                Max SSID len = 31 characters
#
#            per-device OTA flash versions ... instead of LATEST, OTAVersion|ApplyOTA|... (or something)
#            
#            To document: Allow omission of password in import (or a placeholder like "tbd") so it can work with single SSID/pw mode
#            of list-provision, where no DD-WRT device is specified.
#            OTA to update settings/status after complete (or fix this if its not working)
#
#  NICE-TO-HAVES:
#            Insure it's a 2.4GHz network
#            mDNS discovery?
#            OTA from local webserver
#            --ota-version-check=required|skip|try (in case redirect/forwarding would fail)
#
#            new operation apply-list(?)  to apply --settings or --url to probe-list devices, instead of known devices in db
#            Simplify some python2/3 compatibility per: http://python-future.org/compatible_idioms.html
#            DeviceType -- limit provision-list to matching records -- provision-list to choose devices by DeviceType
#            -U option to apply operation to make arbitrary updates in device DB (for use prior to restore)
#            --parallel=<n>  batched parallel firmware updating, n at a time, pausing between batches, or exiting if no more
#            --group for "provision" -- add to group
#            --prompt n,n,n  for use with "provision" to prompt for v,v,v giving individual values like StaticIP
#
#  FUTURE:
#            my.shelly.cloud API integration?
#
######################################################################################################################


from __future__ import print_function
from __future__ import absolute_import
import sys
import os
import re
import time
import json
import argparse
import subprocess
import binascii
import tempfile
import zipfile
import telnetlib
import base64
import getpass
from textwrap import dedent
import csv
import timeit
import importlib
import collections
import copy
import socket

try:
    import requests
except:
    pass

if sys.version_info.major >= 3:
    import urllib.request
    import urllib.parse
    from io import BytesIO
    import collections.abc
    from urllib.parse import urlencode, quote_plus, quote
    from urllib.error import URLError, HTTPError
else:
    from urllib import urlencode
    input = raw_input
    import urllib2
    import urllib
    from StringIO import StringIO
    from urllib2 import HTTPError

version = "1.0010"

required_keys = [ 'SSID', 'Password' ]
optional_keys = [ 'StaticIP', 'NetMask', 'Gateway', 'Group', 'Label', 'ProbeIP', 'Tags', 'DeviceName', 'LatLng', 'TZ', 'Access' ]
default_query_columns = [ 'type', 'Origin', 'IP', 'ID', 'fw', 'has_update', 'settings.name' ] 

all_operations = ( 'help', 'features', 'provision', 'provision-list', 'factory-reset', 'flash', 'import', 'list', 'clear-list', 
                   'ddwrt-learn', 'print-sample', 'probe-list', 'query', 'schema', 'apply', 'identify', 'replace', 'list-versions',
                   'acceptance-test', 'config-test' )

exclude_setting = [ 'unixtime', 'fw', 'time', 'hwinfo', 'build_info', 'device', 'ison', 'has_timer', 'power', 'connected',
        'ext_humidity','ext_switch','ext_sensors','ext_temperature',    #TODO  -- parameter
        'actions',                                                      # handled differently
        'schedule','schedule_rules',                                    # handled differently
        'login','wifi_sta','wifi_sta1','wifi_ap' ]                      # not allowing these, for now, because of password not being present

exclude_from_copy = [ 'actions.names','alt_modes','build_info','calibrated','device.mac','device.hostname','device.num_emeters','device.num_inputs',
                      'device.num_meters','device.num_outputs','device.num_rollers','device.type','fw','hwinfo.batch_id','hwinfo.hw_revision',
                      'unixtime','settings.meters','settings.fw_mode','settings.login.default_username' ]

ota_version_cache = None

init = None
wifi_connect = None
wifi_reconnect = None
urlquote = None
deep_update = None
url_read = None
rpc_post = None
http_post = None
os_stash = {}
stringtofile = None
router_db = None
device_db = None
device_queue = None
factory_device_addr = "192.168.33.1"
labelprinting = None

## For now, device generation 1=old/2=next generation, is this global. Needs refactoring.
dev_gen = 1

####################################################################################
#   Help subsystem
####################################################################################

def help_features( more = None ):
    print(dedent(""" 
                 Features:

                 This utility can be used to provision, maintain, update, and keep an inventory of IoT devices.
                 There are many different operations available, described briefly here, and in more detail
                 in the built-in help for the program.  

                 It can automatically locate new devices that are in the factory reset state, ready to configure.  
                 Each located device can be added to the local WiFi network, using the "provision" operation, or 
                 added to specific other WiFi networks, on a per-device basis, using the "provision-list" operation.  
                 The provision-list operation can also assign different static IP addresses to each device if
                 required.
                 
                 With provision-list, one or two spare DD-WRT routers can be used as the client connection and
                 WiFi access point, automatically configured at each step to match network SSID of the factory
                 reset IoT device and the target SSID and credentials specified in a list of instructions given 
                 to the program.  Note that with two DD-WRT devices, the process is much faster, able to provision
                 1 to 2 target devices per minute.
                 
                 When using the simple provision operation, your computer or laptop will change from one WiFi 
                 network to another (to connect to the target device's WiFi hotspot to configure it).  Using 
                 the more sophisticated provision-list can mean no loss of WiFi connectivity on your computer, 
                 since instructions can be sent to a DD-WRT device to set the WiFi SSID instead.  The provision-list 
                 operation in this mode is generally twice as fast as provision.

                 There are commands to work with the set of instructions used by provision-list to import, view 
                 and clear the list: "import," "list," and "clear-list".  The concept behind importing and managing
                 the list of instructions is so that the program can easily resume where it left off.  The set of
                 "todo" items gets checked off as the program successfully provisions each device and this information
                 persists even if you quit and then restart the program.
                 
                 The provision operation supports only DHCP, while provision-list can setup devices with either
                 DHCP or static IP addresses.  Either operation can additionally command each newly provisioned
                 device to take an OTA firmware update to the LATEST or a specific version of software.
                 
                 With provision-list there are many additional features, including setting the name of the device
                 as it shows up in the phone app and in the settings web UI, plus latitude/longitude and timezone
                 on an individual device basis.  The imported list of instructions can include a "Group" column, 
                 which then allows provision-list to work on a specific set of instructions instead of the entire queue.  
                 A mechanism for automatically printing labels, given a small program provided by the user, is available 
                 with both provision and provision-list, but additional attributes like "Label" (a free-form text string) 
                 can be added to the imported instructions for provision-list.
                 
                 There is a "factory-reset" operation which makes it easy to return a device to factory settings,
                 given it is on the local WiFi network.  The "flash" operation instructs local devices to take
                 an OTA firmware update.

                 A database is maintained with all of the newly provisioned devices.  For an end-user provisioning 
                 devices for use on a local network, the database is tremendously useful for tracking the devices,
                 managing settings and performing OTA updates.

                 For existing devices on the local WiFi network that weren't provisioned using the tool, there is a
                 "probe-list" command to discover their settings and status.  For battery-powered devices that are 
                 only periodically available on the network, the option --access=Periodic lets probe-list run for an 
                 extended period of time looking frequently for the devices.  

                 A powerful "query" operation can report on any information recorded during provisioning or found using
                 the probe operation.  An "apply" operation allows programming the discovered devices with OTA firmware 
                 updates, as well as making arbitrary settings changes using the --settings and --url options.

                 An "identify" operation is available to continually toggle on/off a light or relay, given an IP
                 address, in order to aid in identifying a device.  Useful, for instance, with multiple light bulbs
                 in a lighting fixture.

                 The settings from one device can be copied to a new replacement device using the "replace" operation.
                 Having transfered the settings, it is then possible to use "apply" with --restore to reprovision the
                 replacement device.

                 Use the "list-versions" operation to check the available archived versions of prior firmware for a device.

                 The "acceptance-test" operation checks that devices can be contacted in AP mode (factory reset) and toggles 
                 their relay, without provisioning them.  For a more complete test, choose "config-test" which provisions each 
                 device, toggles their relay, and then returns them factory settings.
                 """) )

def help_operations( more = None ):
    print(dedent(""" 
                 usage: python automagic.py [options] OPERATION

                 OPERATION is one of: help, provision-list, provision, factory-reset, flash, ddwrt-learn, import, 
                                      list, clear-list, print-sample, probe-list, query, apply

                 More detailed information will follow a short description of all of these operations.

                 help               - shows this help guide
                 features           - gives a short explanation of the features of this utility

                 provision          - configure any device(s) found in factory mode onto the current WiFi network

                 import             - import list of instructions for programming with provision-list
                 list               - shows the contents of the imported list of instructions
                 clear-list         - erases the imported instructions for "list" operations
                 provision-list     - configure devices found in factory reset mode onto a list of specified WiFi networks
                 probe-list         - discovers information about devices in the import list (they must specify ProbeIP addresses)

                 ddwrt-learn        - learn identity and settings of dd-wrt devices for use with provision-list
                 factory-reset      - factory reset a device
                 flash              - OTA (over the air) flash new firmware onto a device
                 print-sample       - sends a sample device info record to the custom label printing module (see --print-using)
                 query              - list information from the device database formed from provisioning and probing operations
                 apply              - apply --ota, --url and --settings to devices matching a query from the device database
                 identify           - toggle power on/off on a given device to identify (by ip-address)
                 replace            - copy the settings of one device to another in the device database

                 list-versions      - list prior versions of firmware available for a given device, specified with --device-address (-a)

                 acceptance-test    - checks that devices can be contacted, toggles their relay, to test basic functionality
                 config-test        - provisions each device, toggles their relay, then returns to factory reset state

                 Note that it is inadvisable to run multiple copies of this program while new devices are being
                 powered on to configure.  The program will automatically detect any new device and might attempt
                 to program it from two instances if run on multiple computers simultaneously.
                """))


def more_help( more = None ):
    print(dedent(""" 

                 More help is available for each operation above. Try "help provision" or "help features".
                 You can also try "help all".
                """))

def help_commands( more = None ):
    help_operations( )

def help_help( more = None ):
    print(dedent(""" 
                 help
                 ----
                 Prints the text you are reading now.  Additional help for each operation is available.  Try "help provision" for example.
                 To see all help, try "help all".  An overview of the program's functionalities is available too: "help features".
                """))

def help_provision( more = None ):
    print(dedent(""" 
                 provision
                 ---------
                 The provision operation is used to provision devices to attach to the same WiFi network used by the laptop (or desktop)
                 computer where the program is run.  Its functionality is limited in comparison to the provision-list operation, since all
                 discovered devices will be attached to the same local network.

                     --ssid                      SSID of the local WiFi network.  Specifying on the command line removes the step to 
                                                 confirm it is the proper one...  Make sure it is a 2.4GHz network and the same one the
                                                 computer is using.

                     --time-to-wait (-w)         Time to wait on each pass looking for new devices, 0 for just once

                     --ota PATH                  Apply OTA update of firmware after provisioning. PATH should specify an http path to
                                                 the firmware, or "LATEST".

                     --ota-timeout (-n)          Time in seconds to wait on OTA udpate. Default 300 (5 minutes).

                     --prefix STRING             SSID prefix applied to discovering new devices (default: shelly)

                     --time-to-pause (-p)        Time to pause after various provisioning steps

                     --toggle                    After each device is provisioned, toggle it on/off repeatedly until it is unplugged.

                     --cue                       After each device is provisioned, wait for the user to press enter before continuing.

                     --print-using PYTHON-FILE   For each provisioned device, call a function "make_label," passing all information about
                                                 the newly provisioned device as a python dict, as the single argument to make_label().

                                                 ex: def make_label( dev_info ):
                                                         print( repr( dev_info ) )

                     --settings N=V,N=V...       Supply LatLng or other values to apply during provisioning step.  Supported attributes:
                                                 DeviceName, LatLng, TZ
                """))

def help_provision_list( more = None ):
    print(dedent(""" 
                 provision-list
                 --------------
                 List provisioning is ideal for pre-configuring a large number of IoT devices intended to be deployed on different WiFi
                 networks. An imported list of SSIDS will determine for which networks the devices will be provisioned.

                 In order to configure and verify each device, making sure it is able to connect to its target SSID, a spare DD-WRT 
                 router is used to temporarily create a network with the proper SSID and credentials.

                     ex: python automagic.py -N myddwrt provision-list

                 Before using provision-list, some setup work is required. See ddwrt-learn and import command descriptions.

                 Options for use with provision-list:

                     --ddwrt-name (-N)           This required option specifies the name of a dd-wrt device, as learned 
                                                 with the ddwrt-learn command.  The dd-wrt device will be used to configure 
                                                 the target IoT devices.

                                                 A second instance of this same option can be used to identify a second dd-wrt
                                                 device for use during the provisioning operation.  This will speed up the
                                                 operation by over 2X.  When two dd-wrt devices are available, there is no need to 
                                                 switch a single device between AP and client modes, which is a time-consuming
                                                 process.  It is highly recommended to use the two-device configuration.

                     --group (-g) GROUP          Limit the operation to the set of instructions imported with the given "Group" ID.
                                                 If --group/-g is not specified, ALL imported instructions will be used.

                     --time-to-wait (-w) SECS    Time to wait between each pass before looking for more new devices.

                     --verbose (-v)              Give verbose logging output (repeating -v increases verbosity, use -vvv or more for debug).

                     --ota PATH                  Apply OTA update of firmware after provisioning. PATH should specify an http path to
                                                 the firmware, or "LATEST".

                     --ota-timeout (-n)          Time in seconds to wait on OTA udpate. Default 300 (5 minutes).

                     --device-db FILE.json       This file will hold information about each device that has been provisioned or probed.

                     --device-queue FILE.json    Specifies the name of the .json file that has the list of devices queued up by the
                                                 import command. The default name is provisionlist.json.

                     --ddwrt-file FILE.json      File to contains definitions of dd-wrt devices created using the --ddwrt-learn 
                                                 command.  The default is ddwrt_db.json.

                     --timing                    Show timing of steps during provisioning.

                     --toggle                    After each device is provisioned, toggle it on/off repeatedly until it is unplugged.

                     --cue                       After each device is provisioned, wait for the user to press enter before continuing.

                     --print-using PYTHON-FILE   For each provisioned device, call a function "make_label," passing all information about
                                                 the newly provisioned device as a python dict, as the single argument to make_label().

                                                 ex: def make_label( dev_info ):
                                                         print( repr( dev_info ) )

                     --prefix STRING             SSID prefix applied to discovering new devices (default: shelly)

                     --time-to-pause (-p)        Time to pause after various provisioning steps

                     --settings N=V,N=V...       Supply LatLng or defaults other values to apply during provisioning step.  Supported 
                                                 attributes: DeviceName, LatLng, TZ
                """))

def help_ddwrt_learn( more = None ):
    print(dedent(""" 
                 ddwrt-learn
                 -----------
                 Set up a DD-WRT router in access point mode, with DHCP server disabled, telnet enabled, and ssh enabled.  Use the 
                 username "admin" and choose a password you are okay with storing in plaintext for use by this program. Static wan 
                 address 192.168.33.10, gateway 192.168.33.1, subnet mask 255.255.255.0

                 IMPORTANT: On setup page, disable DHCP, enable forced DNS redirection, and on 
                 Services page, disable Dnsmasq entirely.

                 When the device is configured, run ddwrt-learn.

                      ex: python automagic.py ddwrt-learn -N sh1 -p all4shelly -e 192.168.1.1

                 Change the device WiFi setting to client mode and repeat.

                 This has been tested with Broadcom-based (Linksys) routers.

                 Options for use with provision-list:

                     --ddwrt-name (-N)           Name of device to learn.  (required)

                     --ddwrt-file                File to contains definitions of dd-wrt devices created using the --ddwrt-learn 
                                                 command.  The default is ddwrt_db.json

                     --ddwrt-address (-e)        IP address of dd-wrt device to learn about.  (required)

                     --ddwrt-password (-p)       Root password for dd-wrt device.  (required)
                """))

def help_import( more = None ):
    print(dedent(""" 
                 import
                 ------
                 The import command adds instructions to the queue of operations the provision-list command will perform.

                     ex: python automagic.py import --file /tmp/sample.csv

                 Options for use with import: 

                     --file (-f)                 File containing instructions to import. May be .csv or .json (required).

                 There are many possible fields to include in the .csv (or .json) input file.  Only two are required: SSID
                 and Password.  Here is an example of the simplest possible instructions:

                     $ cat /tmp/sample.csv
                     SSID,Password
                     TestNet,abc12cd34
                     OtherNet,zzaabb33

                 The example above would create instructions to program the next two devices discovered via 
                 provision-list with the credentials specified.

                 Optional fields are: StaticIP, NetMask, Gateway, Group, Label, ProbeIP, and Tags.

                     DeviceName                  If specified, the value name of the device will be set.  (settings/name)

                     StaticIP                    Static IP address to assign to the device.  This will disable DHCP and define
                                                 a static address directly to the device.  You must make provisions with the 
                                                 network router to reserve static IP address and insure they are unique to each
                                                 device.  If StaticIP is included, a NetMask must also be specified.

                     NetMask                     A NetMask must be included when a StaticIP is set.  The NetMask determines what
                                                 IP addresses are on the local subnet vs. routed.

                     Gateway                     A Gateway shoule be included when a StaticIP is set.  It is needed for the device
                                                 to reach any services like sntp, to get the time, or to download OTA updates.

                     LatLng                      Latitude and longitude, for sunrise/sunset calculations, in the form lat:lng,
                                                    ex: 30.33658:-97.77775

                     TZ                          Timezone info, in the form tz_dst:tz_dst_auto:tz_utc_offset:tzautodetect, 
                                                    ex: False:True:-14400:True

                     Group                       A Group can be assigned to an imported record, and later used to select a subset
                                                 of records for operations like provision-list.  See the --group option.

                     Tags                        A set of comma-delimited tags can be assigned to an imported record and used in 
                                                 a similar fashion to the "Group" field.  See the --match-tag option. This field
                                                 must be quoted if it contains commas.

                     Label                       The Label field is useful with the feature for printing a label when each device
                                                 is provisioned.  It is a free-form text field.  See --print-using option, and
                                                 print-sample operation.

                     ProbeIP                     Devices already on the local network, not needing provisioning, can be imported
                                                 and managed using this program.  To import devices, set the ProbeIP to the device's
                                                 IP address and use the probe-list operation.

                     Access                      Defines whether a device should be expected on the network continually, or, like
                                                 some battery powered devices, only periodically. Values: Continuous or Periodic.
                                                 Works in conjunction with ProbeIP and the probe-list operation to find periodic 
                                                 devices which take much longer to discover.
                """))

def help_list( more = None ):
    print(dedent(""" 
                 list
                 ----
                 The list operator prints the pending operations in the device queue, imported using the import operation, and
                 consumed using provisision-list.

                     --group (-g) GROUP          Limit the operation to the set of instructions imported with the given "Group" ID.
                                                 If --group/-g is not specified, ALL imported instructions will be listed.
                """))

def help_clear_list( more = None ):
    print(dedent(""" 
                 clear-list
                 ----------
                 Erase the entire list of pending operations.
                """))

def help_probe_list( more = None ):
    print(dedent(""" 
                 probe-list
                 ----------
                 The probe-list operation gathers information about devices queued using the import command, with the ProbeIP field 
                 specifying their IP addresses existing on the local network. 

                     --query-conditions          Use query-conditions to filter the devices that will be probed.  The query-conditions 
                                                 option is a comma-delimited list of name=value pairs.

                     --group (-g) GROUP          Limit the operation to the set of instructions imported with the given "Group" ID.
                                                 If --group/-g is not specified, ALL imported instructions will be probed.

                     --match-tag (-t)            Limit the query operation to devices with a selected tag.

                     --access Periodic|ALL|Co... Only probe devices that with Access=Continuous (default), Periodic, or ALL

                     --refresh                   Refresh the db with attributes for all previously probed devices, rather than 
                                                 using the import command.
                """))

def help_query( more = None ):
    print(dedent(""" 
                 query
                 -----
                 Print information about devices in the device database.  The device database is comprised of all devices that have
                 been provisioned by the provision-list operation, and any imported using the probe-list operation.

                     --query-columns             A comma-delimited list of columns to display. To learn what columns are available,
                                                 use the schema operation.

                     --query-conditions          Use query-conditions to filter the devices reported by the query operation.  The
                                                 query-conditions option is a comma-delimited list of name=value pairs.  Use the
                                                 schema operation to list the available columns.

                     --group (-g) GROUP          Limit the operation to the set of devices with the given "Group".

                     --match-tag (-t)            Limit the query operation to devices with a selected tag.

                     --set-tag (-T)              Add a specified tag to each selected device.

                     --delete-tag                Delete a specified tag from each selected device.

                     --refresh                   Refresh status and settings stored in the device DB for queried devices.
                """))

def help_schema( more = None ):
    print(dedent(""" 
                 schema
                 ------
                 The schema operaion displays the column list available for query-conditions and query-columns.  It scans the device 
                 database that consists of all devices discovered using the provision-list and probe-list operations.

                     --query-conditions          Use query-conditions to filter the devices reported by the schema operation.  The
                                                 query-conditions option is a comma-delimited list of name=value pairs.

                     --query-columns             A comma-delimited list of columns to match.

                     --group (-g) GROUP          Limit the operation to the set of devices with the given "Group".

                     --match-tag                 Limit the schema operation to devices with a selected tag.
                """))

def help_apply( more = None ):
    print(dedent(""" 
                 apply
                 -----
                 The apply operation is used to apply OTA updates or other settings to devices in the device database.  It functions
                 like the query command, plus additional options: --ota and --url.

                     --query-conditions          Use query-conditions to filter the devices affected by the apply operation.  The
                                                 query-conditions option is a comma-delimited list of name=value pairs.

                     --query-columns             A comma-delimited list of columns to display. To learn what columns are available,
                                                 use the schema operation.

                     --group (-g) GROUP          Limit the operation to the set of instructions imported with the given "Group" ID.

                     --match-tag (-t)            Limit the query operation to devices with a selected tag.

                     --set-tag (-T)              Add a specified tag to each selected device.

                     --delete-tag                Delete a specified tag from each selected device.

                     --ota PATH                  Apply OTA update of firmware after provisioning. PATH should specify an http path to
                                                 the firmware, or "LATEST".

                     --ota-timeout (-n)          Time in seconds to wait on OTA udpate. Default 300 (5 minutes).

                     --time-to-pause (-p)        Time to pause after various provisioning steps

                     --delete-device DEVICE-ID   Provide device ID or "ALL" to delete queries devices from the device db.

                     --restore-device DEVICE-ID  Restores specified device, or with "ALL," those matching the -Q query to their settings
                                                 stored in the device db.

                     --url                       The --url option specifies an URL fragment like "/settings/?lat=31.32&lng=-98.324" to 
                                                 be applied to each matching device.  The --url option can be repeated multiple times.  
                                                 The Device IP will be prefixed to each specified URL fragment to produce a complete URL 
                                                 like "http://192.168.1.10//settings/?lat=31.32&lng=-98.324"

                     --settings N=V,N=V...       Supply LatLng or other values to apply to all matching devices.  Supported attributes:
                                                     DeviceName, LatLng, TZ

                     --dry-run                   When used with --restore-device, --url, and --settings, displays, rather than executes, 
                                                 the steps (urls) which would be applied to each matching device.

                     --refresh                   Refresh status and settings stored in the device DB for queried devices.  (Automatically
                                                 applied after any --url, or --settings operation not specifying --dry-run.)

                     --access Periodic|ALL|Co... Only apply changes to devices that with Access=Continuous (default), Periodic, or ALL
                """))

def help_factory_reset( more = None ):
    print(dedent(""" 
                 factory-reset
                 -------------
                 Performs a factory reset on the specified device.

                     --device-address (required) Address or DNS name of target device
                """))

def help_identify( more = None ):
    print(dedent(""" 
                 identify
                 --------
                 Toggles power on/off on a device to identify which device holds the specified address

                     --device-address (required) Address or DNS name of target device
                """))

def help_flash( more = None ):
    print(dedent(""" 
                 flash  
                 -----
                 The flash operation flashes firmware onto a specified device. (You can also use the --ota option with the "apply" operation
                 to flash multiple devices.)

                     --device-address (required) Address or DNS name of target device

                     --ota PATH                  Apply OTA update of firmware after provisioning. PATH should specify an http path to
                                                 the firmware, or "LATEST".

                     --ota-timeout (-n)          Time in seconds to wait on OTA udpate. Default 300 (5 minutes).

                     --time-to-pause (-p)        Time to pause after various provisioning steps
                """))

def help_print_sample( more = None ):
    print(dedent(""" 
                 print-sample
                 ------------
                 The print-sample operation is used to test the label printing feature and the --print-using option.

                     --print-using PYTHON-FILE   For each provisioned device, call a function "make_label," passing all information about
                                                 the newly provisioned device as a python dict, as the single argument to make_label().

                                                 ex: def make_label( dev_info ):
                                                         print( repr( dev_info ) )
                """))

def help_replace( more = None ):
    print(dedent(""" 
                 replace
                 -------
                 Can be used to copy the settings in the device database from one device to another.  It only affects the settings in
                 the database.  After completing the replace operation, use the --restore-device option to the apply operation, in order to 
                 reprogram the replacement device.

                     python automagic.py replace --from-device 7FB210446B27 --to-device 537B3C3F8823
                     python automagic.py apply --restore-device 537B3C3F8823
                """))
          
def help_acceptance_test( more = None ):
    print(dedent(""" 
                 acceptance-test
                 ---------------
                 The "acceptance-test" operation checks that devices can be contacted in AP mode (factory reset) and toggles 
                 their relay, without provisioning them. 

                     python automagic.py acceptance-test
                """))

def help_config_test( more = None ):
    print(dedent(""" 
                 config-test
                 -----------
                 Provisions each device, toggles their relay, and then returns them factory settings.

                     python automagic.py config-test
                """))

def help_list_versions( more = None ):
    print(dedent(""" 
                 list-versions
                 -------------
                 Lists prior versions of firmware available for a given device.

                     python automagic.py list-versions -a 192.168.1.122
                """))

def example_provision_1():
    print("""
             Example provision-1
             -------------------
             Use the simple "provision" operation to find all new (or freshly reset to factory state) devices
             and add them on the current network:


                 $ python automagic.py provision --time-to-wait 300 --cue

                 Found current SSID BP-AUX. Please be sure this is a 2.4GHz network before proceeding.
                 Connect devices to SSID BP-AUX? (Y/N)> y
                 Waiting to discover a new device
                 Ready to provision shelly1-28a752 with { ... }
                 Confirmed device shelly1-28a752 on BP-AUX network
                 Press <enter> to continue

                 Waiting to discover a new device
                 ^C

                 Attempting to reconnect to BP-AUX after failure or control-C

             With the --time-to-wait option set (instead of the default 0) the program will wait up to 300s,
             (5 minutes) for another device to appear in the factory reset state.  The default behavior looks
             just once for any other devices before quitting.

             The --cue option added the "Press <enter> to continue" feature shown above.  Omit it if you don't
             need the program to wait on user interaction.  Without it you can plug in a number of new devices
             and have each one automatically provisioned.
          """ )

def example_provision_2():
    print("""
             Example provision-2
             -------------------
             Both provision and provision-list include the ability to print out labels as each device is 
             provisioned, and to toggle the device's relay to help identify which device was just set up:

                 $ python automagic.py provision --toggle --print-using custom_label

             With that "--toggle" feature, you'll hear each device go "click-click-click" after provisioning
             is complete.  The program waits until the device is unplugged before looking for another 
             device to configure.

             Here's an example of the structure of the custom_label.py program referenced above.  This version
             wouldn't actually print a label, but would instead use pythons "repr" feature to display all of
             the available attributes.

                 $ cat custom_label.py
                 def make_label( dev_info ):
                     print( repr( dev_info ) )

             See also: "help print-sample"
          """ )

def example_provision_3():
    print("""
             Example provision-3
             -------------------
             Though the provision command isn't as sophisticated as provision-list, there are features to do
             addtional setup beyond just making the WiFi connection to each new device.  Attributes like timezone
             and latitude/longitude which are likely to be the same on many devices can be provided with the 
             --settings option:

                 $ python automagic.py provision --settings TZ=True:True:-14401:False,LatLng=30.33658:-97.77775

             The two settings, TZ and LatLng are separated by a comma.  Each setting has multiple parts, separated by
             colons (:).  TZ has the components tz_dst:tz_dst_auto:tz_utc_offset:tzautodetect, specifying whether
             daylight saving time is active, whether it is automatically set, the offset from UTC, and whether the
             timezone is detected automatically.  LatLng is the latitude and longitude, separated by a colon: 
                 30.33658:-97.77775
          """ )

def example_provision_list_1( ):
    print("""
             Example provision-list-1
             ------------------------
             The provision-list operation uses an imported set of instructions in order to provide different configuration
             information for a series of devices as they are discovered and configured.  It builds on the functionality
             seen using the simpler "provision" operation.  It takes at least two steps to make use of the enhanced
             capabilities:

                 $ python automagic.py import -f my-device-list.csv

                 $ python provision-list

             The instructions imported from the file my-device-list.csv are carried out, in order, and applied to each
             device that is detected by the provision-list operation.

             Here's an example of what data could be found in the example my-device-list.csv above:

                 $ cat my-device-list.csv
                 StaticIP,Gateway,NetMask,TZ,LatLng,SSID,Password
                 192.168.1.121,192.168.1.254,255.255.192.0,True:True:-14401:False,30.33658:-97.77775,TestNet,aasfni4fs43f
                 192.168.1.122,192.168.1.254,255.255.192.0,True:True:-14401:False,30.33658:-97.77775,TestNet,aasfni4fs43f
                 192.168.1.123,192.168.1.254,255.255.192.0,True:True:-14401:False,30.33658:-97.77775,TestNet,aasfni4fs43f
          """ )

## TODO: Now do dd-wrt based provision-list operation...


def example_factory_reset_1():
    print("""
             Example factory-reset-1
             -----------------------
             The factory-reset feature is pretty self-explanatory.  It's a easy way to reset a device if you need to start
             over with the provisioning process:

                 $ python automagic.py factory-reset -a 192.168.1.121
          """ )

def example_flash_1():
    print("""
             Example flash-1
             ---------------
             With the flash operation, you can upgrade the firmware of a device.  For multiple devices see the "apply"
             operation.

                 $ python automagic.py flash --device-address 192.168.1.121 --ota LATEST


             The LATEST keyword requests the latest firmware.  You can provide a specific version with an http address,
             instead:

                 $ python automagic.py flash --device-address 192.168.1.121 --ota http://archive.shelly-tools.de/version/v1.1.10/SHSW-1.zip

          """ )

def example_import_1():
    print("""
             Example import-1
             ----------------
             Use import to append instructions to the "to-do" list of what needs to be provisioned, prior to running provision-list.  The
             input file can be formatted as csv or JSON.  The entire list of available attributes (columns) is listed when you use 
             "help import".

                 $ python automagic.py help import

                 $ vi my-device-list.csv
                   ...

                 $ python automagic.py import -f my-device-list.csv

                 $ python automagic.py list
                 <to-do list is displayed>

          """ )

def example_list_1():
    print("""
             Example list-1
             --------------
             The list operation is used to see what instructions for provision-list (or probe-list) are pending or have been completed.

                 $ python automagic.py list -g Set1
                 Group Set1 has no list entries ready to provision. Use import to specify some provisioning instructions.

                 List of devices for probe-list or provision-list operation
                 Group SSID    Password     StaticIP      NetMask       Gateway       InsertTime    CompletedTime
                 ----- ------- ------------ ------------- ------------- ------------- ------------- -------------
                 Set1  TestNet aasfni4fs43f 192.168.1.121 255.255.192.0 192.168.1.254 1627513668.29 1627513754.31
                 Set1  TestNet aasfni4fs43f 192.168.1.122 255.255.192.0 192.168.1.254 1627513668.29
                 Set1  TestNet aasfni4fs43f 192.168.1.123 255.255.192.0 192.168.1.254 1627513668.29

             The first entry, 192.168.1.121, has been completed, indicated by the value in the CompletedTime column. The other two
             entries are to-be-done.  The "-g Set1" option instructed the "list" operation to display only instructions in the group
             "Set1".  See "help import" to see how to specify groups.
          """ )

def example_clear_list_1():
    print("""
             Example clear-list-1
             --------------------
             Use "clear-list" to erase the pending instructions in the to-do list:

                 $ python automagic.py clear-list

          """ )

def example_print_sample_1():
    print("""
             Example print-sample-1
             ----------------------
             The provision and provision-list operations support an option to print labels to identify each device as it is 
             provisioned.  Printing is handled by a user-supplied python program.  Use "print-sample" to test the printing
             program:

                 $ python automagic.py print-sample --print-using custom_label

             The information sent to the user-supplied program will include a JSON attribute { "TestPrint": True }, in 
             addition to sample attributes similar to what an actual provisioned device will included.
          """ )

def example_ddwrt_learn_1():
    print("""
             Example ddwrt-learn-1
             ---------------------
             One or two DD-WRT routers can be used to speed up the provision-list operation.  The DD-WRT routers must be
             detected and "learned" by the program before use by provision-list.  Learning the DD-WRT routers is accomplished
             using "ddwrt-learn":

                 $ python automagic.py ddwrt-learn --ddwrt-name DEV1 --ddwrt-address 192.168.1.224 --ddwrt-password TempPassWd

             Each of the options, --ddwrt-name, --ddwrt-address, and --ddwrt-password are required.  The --ddwrt-name option is 
             used to supply a name for the router being learned.  It will be referenced in provision-list operations to follow.

             --ddwrt-address specifies the IP address of the DD-WRT router to be detected and learned.
             --ddwrt-password is the root password used to connect to the router using telnet.
          """ )

def example_probe_list_1():
    print("""
             Example probe-list-1
             --------------------
             The program maintains a database of all devices set up using the provision and provision-list commands.  It is also 
             possible to add devices to the database by probing existing devices on your local network.  Supply the device IP
             addresses using the "import" operation, specifying a "ProbeIP" attribute, then run "probe-list":

                 $ python automagic.py import -f my-probe-list.csv

                 $ python automagic.py probe-list
                   ...

                 $ cat my-probe-list.csv
                 ProbeIP
                 192.168.1.121
                 192.168.1.122
                 192.168.1.123
                
          """ )

def example_query_1():
    print("""
             Example query-1
             ---------------
             To query the database built from all of the provisioned and probed devices, use the query operation:

                 $ python automagic.py query

             With no other options, query will show all of the devices and certain default attributes on each line:

                 type     Origin         IP             ID           fw                                   has_update name
                 -------- -------------- -------------- ------------ ------------------------------------ ---------- --------------------
                 SHSW-PM  probe-list     192.168.252.11 98xxxxxxx126 20210323-105928/v1.10.1-gf276b51     False      fern_bed_water
                 SHRGBW2  probe-list     192.168.53.6   B4xxxxxxx98B 20201019-101619/v1.9.0-rc1@04ed984d  True       rgbw-fence-right
                 SHBDUO-1 probe-list     192.168.53.3   98xxxxxxxFD9 20210429-100125/v1.10.4-g3f94cd7     True       None
                 SHBDUO-1 probe-list     192.168.53.9   98xxxxxxxC5A 20210429-100125/v1.10.4-g3f94cd7     True       None
                 SHHT-1   probe-list     192.168.54.1   3Cxxxxxxx39F 20201124-091711/v1.9.0@57ac4ad8      False      None
                 SHSW-1   provision-list 192.168.1.121  ECxxxxxxx751 20210429-100340/v1.10.4-g3f94cd7     False      None               

             You can modify the list of displayed columns with the -q option:

                 $ python automagic.py query -q type,fw,settings.name

             Each column is actually a path that traverses JSON objects, and matches the first or most explicit attribute.  In the 
             example above, settings.name is a different attribute than just "name".  More on the paths and attributes is explained
             in example-schema-1

             The records displayed must match any name-value pairs passed to the -Q option:

                 $ python automagic.py query -q IP,type -Q type=SHBDUO-1
                 IP             type
                 -------------- --------
                 192.168.53.1   SHBDUO-1
                 192.168.53.2   SHBDUO-1
                 192.168.53.3   SHBDUO-1
                 192.168.53.9   SHBDUO-1
          """ )

def example_schema_1():
    print("""
             Example schema-1
             ----------------
             The schema operation is used to explore the JSON data and paths which are available to the query and apply operations. The
             organization of the data is dynamically assembled from the devices that are provisioned and/or probed.  Try:

                 $ python automagic.py schema -vv

             This will output many lines of data similar to the following:

                 status.temperature_status:
                     status.temperature_status [ SHSW-PM, SHSW-25 ]

             This indicates that devices of type SHSW-PM and SHSW-25 both provide data in the JSON read from the device in the form...

                 "status": {
                     "temperature_status": "Normal",...
                 }

             The term status.temperature_status can be used with the query operation in either -q or -Q options.

                 $ python automagic.py query -Q status.temperature_status=Normal

             If temperature_status is unique (found only in one place, under the status object) then this is equivalent:

                 $ python automagic.py query -Q status.temperature_status=Normal

             The -q option works with the schema operator, too, limiting the output to only attributes that match:

                 $ python automagic.py schema -vv -q state
                 settings.rollers.0.state:
                     settings.rollers.[n].state [ SHSW-21, SHSW-25 ]
                 state:
                     status.valves.[n].state [ SHGS-1 ]
                     settings.rollers.[n].state [ SHSW-21, SHSW-25 ]
                 status.valves.0.state:
                     status.valves.[n].state [ SHGS-1
          """ )

def example_apply_1():
    print("""
             Example apply-1
             ---------------
             You can use the "apply" operator to make modifications to devices that match query parameters in the device database.  For example,
             performing an OTA update to all SHSW-1 devices:

                 $ python automagic.py apply -Q type=SHSW-1 --ota=LATEST --dry-run

             With the --dry-run option, the above command wouldn't actually perform the updates, but instead shows the steps it would perform.

             Other options available with apply include --settings and --url.  More information is available using "help apply".  Note that
             without the -Q option to limit this OTA update to only one device type, it would be applied to every device in the device DB.
          """ )

def example_apply_2():
    print("""
             Example apply-2
             ---------------
             Use the --settings option with the apply operation in order to change the name, timezone, or latitude/longitude of one
             or more devices:

                 $ python automagic.py apply -Q ID=7FB210446B27 --settings TZ=True:True:-14401:False,LatLng=30.33658:-97.77775

             As with --ota, there's an option, --dry-run, to see what changes would be made without actually applying them.  Here we
             show -Q using the ID (mac address) of a specific device to insure the apply operation affects it only.
          """ )

def example_apply_3():
    print("""
             Example apply-3
             ---------------
             With the --url option, the apply operation can control or configure any device(s) in any way imaginable.  The --url option 
             takes a parameter which is an url fragment to be appended after http://<device-address>/.  Using direct knowledge of the 
             device's web API, construct 

                 $ python automagic.py apply -Q ID=7FB210446B27 --url "relay/0/?turn=on" -vv

             With the -vv option you can also see the responses from the device(s).
          """ )

def example_apply_4():
    print("""
             Example apply-4
             ---------------
             The apply operation can be used to restore settings on a device using the settings stored in the device DB.  In this example
             we first refresh the stored information using query --refresh, then generate all of the URLs which would restore the device's
             state using apply --restore-device:


                 $ python automagic.py query -Q ID=7FB210446B27 --refresh
                 Refreshing info from network devices
                 type     Origin         IP             ID           fw                                   has_update name
                 -------- -------------- -------------- ------------ ------------------------------------ ---------- --------------------
                 SHSW-1   probe-list     192.168.51.1   7FB210446B27 20210415-125832/v1.10.3-g23074d0     True       counter_lights

             Then...

                 $ python automagic.py apply --restore-device 84F3EB9F5C4D --dry-run
                 type     Origin         IP             ID           fw                                   has_update name
                 -------- -------------- -------------- ------------ ------------------------------------ ---------- --------------------
                 SHSW-1   probe-list     192.168.51.1   84F3EB9F5C4D 20210415-125832/v1.10.3-g23074d0     True       counter_lights

                 http://192.168.51.1/settings/cloud?connected=False&enabled=False
                 http://192.168.51.1/settings/ap_roaming?threshold=-70&enabled=False
                 http://192.168.51.1/settings/?sntp_server=time.google.com
                 http://192.168.51.1/settings/?mqtt_enable=True&mqtt_reconnect_timeout_min=2.0&mqtt_upd...
                 <snip>

             In reality, 20 more lines of output would be displayed, including all of the device's "action" URLs.  Of course, remove
             the --dry-run, and this would actually apply all of the updates to the device, rather than just displaying them.

             Note that the --restore-device option takes a mandatory argument with the device's ID (mac address).  This is to insure that
             the user definitely intends to affect the specific device chosen.  Another option is to use -Q to choose a set of devices, and
             --restore-device ALL, which overrides the protection that otherwise limits the operation to a single device:

                 $ python automagic.py apply --restore-device ALL --Q type=SHSW-1

             This would restore the configuration of every Shelly 1 found in the device DB.

          """ )

def example_identify_1():
    print("""
             Example identify-1
             ------------------
             The identify operation turns a light or relay on/off repeatedly to help identify which device is at the specified address.
             The -a option is required:

                 $ python automagic.py identify -a 192.168.51.1

             The device will continue flashing until the identify operation is terminated with ^C (control-C) or the device becomes 
             unreachable (unplugged or disconnected from the network).
          """ )

def example_replace_1():
    print("""
             Example replace-1
             -----------------
             The replace operation copies settings in the device database from one device to another.  Note that it only affects the device
             DB.  To complete the process of configuring a new device to replace an old one requires subsequently using --restore-device and
             the apply operation.


                 $ python automagic.py import -f my-probe-list.csv               # import IP addresses to probe
                 $ python automagic.py probe-list                                # learn devices
                 $ python automagic.py provision                                 # discover new device
                 $ python automatic.py list                                      # look up IDs of devices

                 $ python automagic.py replace --from-device 7FB210446B27 --to-device 537B3C3F8823
                 $ python automagic.py apply --restore-device 537B3C3F8823
          """ )

def example_list_versions_1( ):
    print(dedent(""" 
             Example list-versions
             ---------------------
             The list-versions operation shows the prior versions of firmware available for a given device.

                 $ python automagic.py list-versions -a 192.168.1.122
                """))

def example_acceptance_test( ):
    print(dedent(""" 
             acceptance-test
             ---------------
             The "acceptance-test" operation checks that devices can be contacted in AP mode (factory reset) and toggles 
             their relay, without provisioning them. 

                 $ python automagic.py acceptance-test
                """))

def example_config_test( ):
    print(dedent(""" 
             config-test
             -----------
             Provisions each device, toggles their relay, and then returns them factory settings.

                 $ python automagic.py config-test
                """))

def help_example( more = None ):
    if not more:
        print( "To recall a specific example: ")
        print( "help example <operation>_n" ) 
        print( "     ex: help example provision-list-1" )
        print( )
    else:
        try:
            eval( 'example_' + more.replace('-','_') + '(  )' )
        except:
            pass
        try:
            eval( 'example_' + more.replace('-','_') + '_1(  )' )
        except:
            print( "There is no example titled " + more )
            print( )

def help_examples( more = None, need_prompt = False ):
    no_example = ( 'help', 'features' )
    if not more:
        print( "This will step through examples for all operations, stopping between each one.  To look at examples for a specific " )
        print( "operation try 'help examples <operation>'.  Additionally, you can recall a specific example with: ")
        print( "help example <operation>_n" ) 
        print( "     ex: help example provision-list-1" )
        print( )
        print( )
        for e in all_operations:
            if e not in no_example:
                #print( e )
                #print( '-' * len( e ) )
                if not help_examples( e, need_prompt ):
                    return False
                need_prompt = True
    else:
        n = 1
        found_any = False
        while n > 0:
            f = 'example_' + more.replace('-','_') + '_' + str( n )
            call = f + '(  )'
            if need_prompt and f in globals( ):
                answer = input( 'Continue?' )
                if answer and answer.upper() not in ('Y','YES'):
                    return False
            try:
                eval( call )
                print( )
                found_any = True
                need_prompt = True
                n += 1
            except:
                n = 0
        if not found_any:
            help_example( more )
    return True

def help_docs( what ):
    if not what:
        help_operations( )
        more_help( )
    elif what[0] == "all":
        help_operations( )
        help_help( )
        help_provision( )
        help_provision_list( )
        help_ddwrt_learn( )
        help_import( )
        help_list( )
        help_clear_list( )
        help_probe_list( )
        help_query( )
        help_schema( )
        help_apply( )
        help_factory_reset( )
        help_identify( )
        help_flash( )
        help_print_sample( )
        help_replace( )
    else:
        arg = '"""' + what[ 1 ] + '"""' if len( what ) > 1 else "None"
        try:
            eval( 'help_' + what[0].replace('-','_') + '( ' + arg + ' )' )
        except KeyboardInterrupt as error:
            pass
        except:
            print( "No help for " + what[0] )
            print( "Try: help operations, help examples, or one of help... " + ', '.join( all_operations ) )

####################################################################################
#   Python 2/3 compatibility functions
####################################################################################

def v2_url_read( s, mode = 't', tmout = 2 ):
    if mode == 'b':
        return urllib2.urlopen( s, timeout = tmout ).read( )
    return urllib2.urlopen( s, timeout = tmout ).read( ).decode( 'utf8' )

def v2_rpc_post( s, data, mode = 't', tmout = 2 ):
    response = urllib2.urlopen( urllib2.Request( s, data ), timeout = tmout )
    if mode == 'b':
        return response.read()
    return response.read().decode( 'utf8' )

def v3_url_read( s, mode = 't', tmout = 2 ):
    return urllib.request.urlopen( s, timeout = tmout ).read( )

def v3_rpc_post( s, data, mode = 't', tmout = 2 ):
    #TODO-KBC - timeout?
    return( requests.post( s, data = data, headers={'Content-Type': 'application/x-www-form-urlencoded'} ).text )

def v2_http_post( url, data, username, password, referrer ):
    post = url_encode( data )
    req = urllib2.Request( url, post )
    base64string = base64.b64encode( '%s:%s' % ( username, password ) )
    req.add_header( "Authorization", "Basic %s" % base64string )
    req.add_header( 'Referer', referrer )
    response = urllib2.urlopen( req )
    return response.read( )

def v3_http_post( url, data, username, password, referrer ):
    return( requests.post( url, data = data, auth = ( username, password ), headers = { 'Referer' : referrer } ) )

def v2_deep_update(d, u):
    for k, v in u.iteritems():
        if isinstance(v, collections.Mapping):
            d[k] = v2_deep_update(d.get(k, {}), v)
        else:
            d[k] = v
    return d

def v3_deep_update(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = v3_deep_update(d.get(k, {}), v)
        else:
            d[k] = v
    return d

def noop( a = "" ):
    return( a )

def compatibility( ):
    global url_read, rpc_post, http_post, urlquote, stringtofile, deep_update

    if sys.version_info.major >= 3:
        url_read = v3_url_read
        rpc_post = v3_rpc_post
        http_post = v3_http_post
        deep_update = v3_deep_update
        urlquote = urllib.parse.quote
        stringtofile = BytesIO
    else:
        url_read = v2_url_read
        rpc_post = v2_rpc_post
        http_post = v2_http_post
        deep_update = v2_deep_update
        urlquote = urllib.quote_plus
        stringtofile = StringIO

####################################################################################
#   PC compatibility functions
####################################################################################

def pc_write_profile( ssid, path ):
    f = open( path, "w" )
    f.write( """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
        <name>""" + ssid + """</name>
	<SSIDConfig>
		<SSID>
                        <hex>""" + str(binascii.b2a_hex(ssid.encode("utf-8")).decode()) + """</hex>
                        <name>""" + ssid + """</name>
		</SSID>
	</SSIDConfig>
	<connectionType>ESS</connectionType>
	<connectionMode>manual</connectionMode>
	<MSM>
		<security>
			<authEncryption>
				<authentication>open</authentication>
				<encryption>none</encryption>
				<useOneX>false</useOneX>
			</authEncryption>
		</security>
	</MSM>
	<MacRandomization xmlns="http://www.microsoft.com/networking/WLAN/profile/v3">
		<enableRandomization>false</enableRandomization>
	</MacRandomization>
</WLANProfile>""")
    f.close()

def pc_quote( s ):
    return '^"' + s.replace("^","^^").replace("&","^&") + '^"'

def pc_get_cmd_output(cmd, key, err):
    output = subprocess.check_output( cmd ).decode( 'utf8' )
    m = re.search( key + ' *:  *(.*)', output )
    if not m:
         eprint( err )
         sys.exit()
    return m.group(1).rstrip()

def pc_get_cred():
    ssid = pc_get_cmd_output( 'cmd /c "netsh wlan show interfaces | findstr SSID"', 'SSID', "Could not identify current SSID" )
    profile = pc_get_cmd_output( 'cmd /c "netsh wlan show interfaces | findstr Profile"', 'Profile', "Could not identify current Profile" )
    pw = pc_get_cmd_output( 'cmd /c "netsh wlan show profile name=' + pc_quote( ssid ) + ' key=clear | findstr Key"', 'Key Content', "Could not determine pasword for network " + ssid )
    return { 'profile' : ssid, 'ssid' : ssid, 'password' : pw }

def pc_wifi_connect( credentials, mstr, prefix = False, password = '', ignore_ssids = {}, verbose = 0 ):
    if prefix:
        print( "Disconnecting from your WiFi to try to discover new SSIDs, because of Windows OS limitations. :-(" )
        # it's necessary to disconnect in order to have wlan show networks show all networks
        subprocess.check_output('cmd /c "netsh wlan disconnect"')
        time.sleep( 5 )    # this sleep may have helped in finding devices which would be missed if show networks runs too soon
        show_networks = subprocess.check_output( 'cmd /c "netsh wlan show networks"' ).decode('utf8')
        network = None
        networks = re.findall( r'SSID .*', show_networks, re.MULTILINE )
        if verbose > 2: print(repr(networks))
        skipped = 0
        for n in networks:
            if verbose > 2: print(repr(n))
            m = re.search( 'SSID  *[0-9][0-9]*  *:  *(' + mstr + '.*)', n, re.IGNORECASE )
            if m and m.group(1) != '':
                if m.group(1).rstrip() not in ignore_ssids:
                    network = m.group(1).rstrip()
                    break
                else:
                    skipped += 1

        if not network:
            if skipped and verbose > 2:
                print( "skipped " + str( skipped ) + " device(s) still showing up on network but previously processed" )
            subprocess.check_output('cmd /c "netsh wlan connect name=' + pc_quote( credentials['profile'] ) + ' "')
            return None
    else:
        network = mstr

    pc_write_profile( network, tempfile.gettempdir() + r"\ntwrk_tmp.xml" )
    subprocess.check_output('cmd /c "netsh wlan add profile filename=' + tempfile.gettempdir() + r'\ntwrk_tmp.xml user=all"')

    subprocess.check_output('cmd /c "netsh wlan connect name=' + pc_quote( network ) + ' "')
    return network

def pc_wifi_reconnect( credentials ):
    subprocess.check_output('cmd /c "netsh wlan connect name=' + pc_quote( credentials['profile'] ) + ' "')
    return True

####################################################################################
#   Mac compatibility functions
####################################################################################

def mac_init( ):
    global os_stash
    import objc
    
    objc.loadBundle('CoreWLAN',
                    bundle_path = '/System/Library/Frameworks/CoreWLAN.framework',
                    module_globals = globals())
    
    os_stash['iface'] = CWInterface.interface()

def mac_get_cred():
    ssid = os_stash['iface'].ssid()
    print( "You will be prompted for your password in order to get WiFi credentials from the current " + ssid + " network.  Press <escape> to abort." )
    time.sleep( .5 )
    pw = subprocess.check_output( """security find-generic-password -ga '""" + ssid + """' 2>&1 1>/dev/null | sed -e 's/password: "//' -e 's/"$//'""", shell=True ).rstrip().decode("ascii")
    if pw == '':
        print( "Could not get wifi password" )
        sys.exit()
    return {'profile' : ssid, 'ssid' : ssid, 'password' : pw }

def mac_wifi_connect( credentials, str, prefix = False, password = '', ignore_ssids = {}, verbose = 0 ):
    passes = 0
    while passes < 5:
        for i in range( 3 ):
            passes += 1
            if prefix:
                networks, error = os_stash['iface'].scanForNetworksWithSSID_error_(None, None)
            else:
                networks, error = os_stash['iface'].scanForNetworksWithName_error_(str, None)
            if networks:
                break
            time.sleep( 1 )
    
        if verbose > 2: print(repr(networks))
    
        if not networks:
            eprint( error )
            return None
    
        found = None
        
        for n in networks:
            if n.ssid() and ( n.ssid().lower().startswith(str.lower()) and prefix or n.ssid() == str ) and n.ssid() not in ignore_ssids:
                found = n
                break

        if found: break

    if not found:
        return None
   
    if found: 
        if verbose > 2: print( 'Detected ' + found.ssid() )
        for i in range(3):
            success, error = os_stash['iface'].associateToNetwork_password_error_(found, password, None)
            if error:
                if verbose > 0: eprint(error)
            else:
                return found.ssid()
    return None

def mac_wifi_reconnect( credentials ):
    return mac_wifi_connect( credentials, credentials['ssid'], prefix = False, password = credentials['password'] )

####################################################################################
#   DD-WRT interactions
####################################################################################

def ddwrt_do_cmd( tn, cmd, prompt, verbose = 0 ):
    if verbose: print( cmd )
    dbg = tn.read_very_eager() # throw away any pending junk
    if verbose > 2: print( dbg )
    tn.write(b"echo ${z}BOT${z};(" + cmd.encode('ascii') + b")  2>/tmp/cmd.err.out\n")
    dbg = tn.read_until(b'####BOT####\r\n',2)   ### consume echo
    if verbose > 2: print( dbg )
    response = tn.read_until(prompt,2).decode('ascii')[:-len(prompt)-1]   #remove prompt
    result = []
    err = ""
    if verbose > 1: print( "[[[" + response.replace("\r","") + "]]]" )
    for line in response.replace("\r","").split("\n"):
        result.append( line )
    tn.write(b"echo ${z}BOT${z};cat /tmp/cmd.err.out\n")
    dbg = tn.read_until(b'####BOT####\r\n',2)   ### consume echo
    if verbose > 2: print( dbg )
    err = tn.read_until(prompt,2).decode('ascii')[:-len(prompt)-1]   #remove prompt
    if verbose > 2: print( err )
    return ( result, err )

def ddwrt_ssh_loopback( node, verbose = 0 ):
    # establish SSH loopback for the purpose of port forwarding
    tn = node[ 'conn' ]
    cmd = 'ssh -y -L' + node['router']['address'] + ':8001:192.168.33.1:80 localhost\n'
    pw_prompt = 'password:'
    shell_prompt = 'root@'
    pw = node['router']['password'] + '\n'

    ddwrt_do_cmd( tn, 'pwd', node['eot'], verbose )

    dbg = tn.read_very_eager()   # throw away any pending junk
    if verbose: print( "(1)" + dbg )
    tn.write(b"echo ${z}BOT${z};(" + cmd.encode('ascii') + b")\n")
    dbg = tn.read_until(pw_prompt.encode('ascii'),10)
    if verbose: print( "(2)" + dbg )
    tn.write(pw.encode('ascii'))
    dbg = tn.read_until(shell_prompt.encode('ascii'),2)
    if verbose: print( "(3)" + dbg )
    dbg = tn.read_very_eager()
    if verbose: print( "(4)" + dbg )
    ddwrt_sync_connection( node, b"PS1="+node['eot']+b"\\\\n;", 2 )
    dbg = tn.read_very_eager()
    if verbose: print( "(5)" + dbg )
    ddwrt_do_cmd( tn, 'pwd', node['eot'] )
    dbg = tn.read_very_eager()
    if verbose: print( "(6)" + dbg )

def ddwrt_get_single_line_result( cn, cmd ):
    ( result, err ) = ddwrt_do_cmd( cn['conn'], cmd, cn['eot'] )
    if err != "":
        raise Exception( err )
    if len( result ) > 2:
        raise Exception( 'multi-line response' )
    return( result[0] )

def ddwrt_sync_connection( cn, btext, tmout ):
    cn['conn'].write( btext + b"z='####';echo ${z}SYNC${z}\n" )
    cn['conn'].read_until( b'####SYNC####\r\n', tmout )
    cn['conn'].read_until( cn['eot'], tmout )

def ddwrt_establish_connection( address, user, password, eot ):
    tn = telnetlib.Telnet( address )
    tn.read_until( b"login: " )
    tn.write( user.encode( 'ascii' ) + b"\n")
    if password:
        tn.read_until( b"Password: " )
        tn.write( password.encode( 'ascii' ) + b"\n" )
    cn = { 'conn' : tn, 'eot' : eot }
    ddwrt_sync_connection( cn, b"PS1="+eot+b"\\\\n;", 20 )
    return cn

def ddwrt_connect_to_known_router( ddwrt_name ):
    if ddwrt_name not in router_db:
        print( 'dd-wrt device ' + ddwrt_name + ' not found. Use ddwrt-learn, or choose another device that is already known.' )
        sys.exit()
    router = router_db[ ddwrt_name ]
    cn = ddwrt_establish_connection( router[ 'address' ], 'root', router[ 'password' ], b'#EOT#' )
    cn[ 'router' ] = router
    et0macaddr = ddwrt_get_single_line_result( cn, "nvram get et0macaddr" )
    if et0macaddr != router[ 'et0macaddr' ]:
        print( 'device currently at ip address ' + router[ 'address' ] + ' is not ' + ddwrt_name )
        sys.exit( )
    cn[ 'current_mode' ] = ddwrt_get_single_line_result( cn, "nvram get wl_mode" )
    return cn

def ddwrt_apply( address, user, password ):
    data = { 'submit_button':'index', 'action':'ApplyTake' }
    http_post( 'http://' + address + '/apply.cgi', data, user, password, 'http://' + address + '/Management.asp' )

def ddwrt_program_mode( cn, pgm, from_db, deletes=None ):
    for k in pgm.keys():
        ddwrt_get_single_line_result( cn, "nvram set " + k + '="' + pgm[k] + '"' )
    mode = pgm[ 'wl_mode' ]
    for k in from_db:
        ddwrt_get_single_line_result( cn, "nvram set " + k + '=' + cn[ 'router' ][ mode ][ k ] )
    if deletes:
        for k in deletes:
            ddwrt_get_single_line_result( cn, "nvram unset " + k )
    ddwrt_get_single_line_result( cn, "nvram commit 2>/dev/null" )
    if cn[ 'current_mode' ] == mode:
        ddwrt_get_single_line_result( cn, "stopservice nas;stopservice wlconf 2>/dev/null;startservice wlconf 2>/dev/null;startservice nas" )
    else:
        cn[ 'current_mode' ] = mode
        ddwrt_apply( cn[ 'router' ][ 'address' ], 'admin', cn[ 'router' ][ 'password' ] )
        print( "changing dd-wrt mode to " + mode + "... configuration sent, now waiting for dd-wrt to apply changes" )
        time.sleep( 5 )
        ddwrt_sync_connection( cn, b'', 20 )
        ddwrt_get_single_line_result( cn, "wl radio off; wl radio on" )

def ddwrt_set_ap_mode( cn, ssid, password ):
    pgm = { 'pptp_use_dhcp' : '1',        'wan_gateway' : '0.0.0.0',         'wan_ipaddr' : '0.0.0.0',               
            'wan_netmask' : '0.0.0.0',    'wan_proto' : 'disabled',          'wl0_akm' : 'psk psk2',                 
            'wl0_mode' : 'ap',            'wl0_nctrlsb' : 'none',            'wl0_security_mode' : 'psk psk2',       
            'wl0_ssid' : ssid,            'wl_ssid' : ssid,                  'wl0_wpa_psk' : password,               
            'wl_mode' : 'ap',             'dns_redirect' : '1',              'dnsmasq_enable' : '0'                  
          } 
    from_db = [ 'wl0_hw_rxchain','wl0_hw_txchain','wan_hwaddr' ]
    deletes = [ 'wan_ipaddr_buf','wan_ipaddr_static','wan_netmask_static', 'wl0_vifs' ]
    ddwrt_program_mode( cn, pgm, from_db, deletes )

def ddwrt_set_sta_mode( cn, ssid ):
    pgm = { 'pptp_use_dhcp' : '0',        'wan_gateway' : '192.168.33.1',    'wan_ipaddr' : '192.168.33.10',                     
            'wan_ipaddr_static' : '..',   'wan_netmask' : '255.255.255.0',   'wan_netmask_static' : '..',                     
            'wan_proto' : 'static',       'wl0_akm' : 'disabled',            'wl0_mode' : 'sta',                     
            'wl0_nctrlsb' : '',           'wl0_security_mode' : 'disabled',  'wl0_vifs' : '',                     
            'wl_mode' : 'sta',            'wl0_ssid' : ssid,                 'wl_ssid' :  ssid,                     
            'dns_redirect' : '1',         'dnsmasq_enable' : '0',            'wan_ipaddr_buf' : '192.168.33.10',          
          }
    from_db = [ 'sta_ifname','wl0_hw_rxchain','wl0_hw_txchain','wan_hwaddr' ]
    deletes = [ 'wl0_wpa_psk' ]
    ddwrt_program_mode( cn, pgm, from_db, deletes )

def ddwrt_learn( ddwrt_name, ddwrt_address, ddwrt_password, ddwrt_file ):
    global router_db
    cn = ddwrt_establish_connection( ddwrt_address, "root", ddwrt_password, b'#EOT#' )
    ddwrt_info = { }
    et0macaddr = ddwrt_get_single_line_result( cn, "nvram get et0macaddr" )
    for term in ( 'sta_ifname', 'wan_hwaddr', 'wl0_mode', 'wl0_hw_txchain', 'wl0_hw_rxchain' ):
        result = ddwrt_get_single_line_result( cn, "nvram get "+term )
        ddwrt_info[term] = result

    if ddwrt_name in router_db:
        old_info = router_db[ ddwrt_name ]
        if old_info[ 'et0macaddr' ] != et0macaddr:
            print( 'Name ' + ddwrt_name + ' is already used for another device: ' + old_info[ 'et0macaddr' ] )
            print( 'Choose a different name and try again.' )
            sys.exit()
        print( "updating info for " + ddwrt_name )
        old_info[ ddwrt_info[ 'wl0_mode' ] ] = ddwrt_info
        router_db[ ddwrt_name ] = old_info
    else:
        router_db[ ddwrt_name ] = { "name" : ddwrt_name, "address" : ddwrt_address, "password" : ddwrt_password ,
                                    "et0macaddr" : et0macaddr, ddwrt_info[ "wl0_mode" ] : ddwrt_info  }
    router_db[ ddwrt_name ][ 'InsertTime' ] = time.time()
    write_json_file( ddwrt_file, router_db )
    print( ddwrt_info[ 'wl0_mode' ] + ' mode learned' )
    if 'ap' not in router_db[ ddwrt_name ]:
        print( 'ap mode has not been detected yet for this ddwrt device. To use it for verification step, configure ap mode and re-learn' )
    elif 'sta' not in router_db[ ddwrt_name ]:
        print( 'sta mode has not been detected yet for this ddwrt device. To use it for configuration step, configure client mode with static wan address 192.168.33.10 and re-learn' )
    else:
        print( 'Device is now fully learned, ready for configuration and verification of target' )

def ddwrt_choose_roles( ddwrt_name ):
    nodes = []
    ap_node = 0
    sta_node = len( ddwrt_name ) - 1
    for node in ddwrt_name:
        nodes.append( ddwrt_connect_to_known_router( node ) )
    ap_capable = []
    sta_capable = []
    current_ap = []
    current_sta = []
    for i in range( len(nodes) ):
        if 'ap' in nodes[i]['router']: ap_capable.append( i )
        if 'sta' in nodes[i]['router']: sta_capable.append( i )
        if 'ap' == nodes[i]['current_mode']: current_ap.append( i )
        if 'sta' == nodes[i]['current_mode']: current_sta.append( i )
    if len( ap_capable ) == 0:
        print( "No AP capable dd-wrt device found. Re-learn the device with it set in AP mode" )
        sys.exit()
    if len( sta_capable ) == 0:
        print( "No client-mode capable dd-wrt device found. Re-learn the device with it set in client mode" )
        sys.exit()
    if len( nodes ) > 1:
        if len( ap_capable ) < 2:
            ap_node = ap_capable[0]
            sta_node = ( ap_node + 1 ) % 2
        elif len( sta_capable ) < 2:
            sta_node = sta_capable[0]
            ap_node = ( sta_node + 1 ) % 2
        elif len( current_ap ) < 2:
            ap_node = current_ap[0]
            sta_node = ( ap_node + 1 ) % 2
        elif len( current_sta ) < 2:
            sta_node = sta_capable[0]
            ap_node = ( sta_node + 1 ) % 2
    return( nodes[ ap_node ], nodes[ sta_node ] )

def ddwrt_discover( cn, prefix ):
    cmd = "site_survey 2>&1"
    ( result, err ) = ddwrt_do_cmd( cn['conn'], cmd, cn['eot'] )
    if not result and err != '':
        eprint( err )
        sys.exit( )
    ret = []
    for n in range( 1, len( result ) ):
        r = re.sub( r'.*SSID\[ *(.*)\] BSSID\[.*', r'\1', result[ n ] )
        if r.lower().startswith( prefix.lower() ):
            ret.append( r )
    return ret

####################################################################################
#   Label interface
####################################################################################

def import_label_lib( print_using ):
    global labelprinting
    labelprinting = importlib.import_module( print_using )
    if 'make_label' not in dir( labelprinting ):
        print( "The module " + print_using + " does not contain a function 'make_label()'." )
        sys.exit( )

def print_label( dev_info ):
    try:
        labelprinting.make_label( dev_info )
    except:
        for i in range(3): print()
        print( "*******************************************" )
        print( "* Failure in custom label printing module *" )
        print( "*      stack trace follows...             *" )
        print( "*******************************************" )
        for i in range(3): print()
        raise

def test_print( ):
    dev_info = {
            "Group": "foo",
            "Brand": "Shelly",
            "IP": "192.168.33.1",
            "ID": "ECFABC746290",
            "TestPrint": True,
            "Label": "Las Vegas, NV. Store #45",
            "SSID": "TestNet",
            "Password": "12xyzab34",
            "StaticIP": "192.168.1.22",
            "NetMask": "255.255.192.0",
            "ConfigStatus" : {
                "Origin": "provision-list",
                "factory_ssid": "shelly1-746290",
                "InProgressTime": 1625598429.713981,
                "CompletedTime": 1625598447.571315,
                "ConfirmedTime": 1625598447.6824908,
            },
            "status": {
                "wifi_sta": {
                    "connected": False,
                    "ssid": "",
                    "ip": "192.168.33.1"
                },
                "mac": "ECFABC746290",
                "update": {
                    "status": "unknown",
                    "has_update": False,
                    "new_version": "",
                    "old_version": "20210429-100340/v1.10.4-g3f94cd7"
                }
            },
            "settings": {
                "device": {
                    "type": "SHSW-1",
                    "mac": "ECFABC746290",
                    "hostname": "shelly1-746290",
                    "num_outputs": 1
                },
                "wifi_ap": {
                    "enabled": False,
                    "ssid": "shelly1-746290",
                    "key": ""
                },
                "wifi_sta": {
                    "enabled": True,
                    "ssid": "TestNet",
                    "ipv4_method": "static",
                    "ip": "192.168.1.22",
                    "gw": None,
                    "mask": "255.255.192.0",
                    "dns": None
                },
                "fw": "20210429-100340/v1.10.4-g3f94cd7",
                "build_info": {
                    "build_id": "20210429-100340/v1.10.4-g3f94cd7",
                    "build_timestamp": "2021-04-29T10:03:40Z",
                    "build_version": "1.0"
                }
            }
        }
    print_label( dev_info )

####################################################################################
#   HTTP / network Utilities
####################################################################################

def url_encode( vals ):
    if type( vals ) == type( { } ):
        return urlencode( dict( [ [ v, vals[ v ] if vals[ v ] != None else '' ] for v in vals ] ) ).replace( 'urls%5B%5D', 'urls[]' )
    else:
        return urlencode( [ ( n, v ) if v != None else ( n, '' ) for ( n, v ) in vals ] ).replace( 'urls%5B%5D', 'urls[]' )

def any_timeout_reason( e ):
    return isinstance( e, socket.timeout ) or \
           'reason' in dir( e ) and ( isinstance( e.reason, socket.timeout ) or \
                str( e.reason ) in (
                    '[Errno 64] Host is down',
                    'urlopen error [Errno 8] nodename nor servname provided, or not known',
                    '[Errno 61] Connection refused',
                    'urlopen error timed out' ) )

def get_url( addr, tm, verbose, url, operation, tmout = 2 ):
    for i in range( 10 ):
        contents=""
        raw_data=""
        if verbose > 2 and operation != '':
            print( 'Attempting to connect to ' + addr + ' ' + operation )
            if verbose > 3:
                print( url )
        try:
            raw_data = url_read( url, tmout = tmout )
            contents = json.loads( raw_data )
        except HTTPError as e:
            print('in get_url, reading: ', url)
            print('Error code:', e.code)
            print( e.read( ) )
        except BaseException as e:
            if any_timeout_reason( e ):
               pass   ### ignore timeout
            else:
               if verbose > 2 or i > 3: print( 'error in get_url: ' + repr( str( e ) ) )
       
        if contents:
            if verbose > 3:
                print( repr( contents ) )
            return contents
        time.sleep( tm )

    print( "Failed ten times to contact device at " + addr + ". Try increasing --time-to-pause option, or move device closer" )
    if raw_data: print( "Raw results from last attempt: " + raw_data )
    return None

def set_wifi_get( address, ssid, pw, static_ip, ip_mask, gateway ):
    if static_ip:
        gw = ( "&gateway=" + gateway ) if gateway else ''
        return "http://" + address + "/settings/sta/?enabled=1&ssid=" + urlquote(ssid) + "&key=" + urlquote(pw) + "&ipv4_method=static&ip=" + static_ip + "&netmask=" + ip_mask + gw
    else:
        return "http://" + address + "/settings/sta/?enabled=1&ssid=" + urlquote(ssid) + "&key=" + urlquote(pw) + "&ipv4_method=dhcp"

def set_wifi_post( address, ssid, pw, static_ip, ip_mask, gateway ):
    if static_ip:
        gw = ( '"gw":"' + gateway + '", ') if gateway else ''
        return ( 'http://' + address + '/rpc', 
                 '{ "id":1, "src":"user_1", "method":"WiFi.SetConfig", "params":{"config":{"sta":{"ssid":"' + ssid + '", "pass":"' + pw + '", ' +
                 '"ipv4mode":"static", "netmask":"' + ip_mask + '", ' + gw + '"ip":"' + static_ip + '", "enable": true, "nameserver":null}}}}' )
    else:
        return ( 'http://' + address + '/rpc', 
                 '{ "id":1, "src":"user_1", "method":"WiFi.SetConfig", "params":{"config":{"sta1":{"ssid":"' + ssid + '", "pass":"' + pw + '", "enable": true}}}}' )

def disable_ap_post( address ):
    return ( 'http://' + address + '/rpc', 
             '{ "id":1, "src":"user_1", "method":"WiFi.SetConfig", "params":{"config":{"ap":{"enable": false}}}}' )

def disable_BLE_post( address ):
    return ( 'http://' + address + '/rpc', 
             '{ "id":1, "src":"user_1", "method":"BLE.SetConfig", "params":{"config":{"enable": false}}}' )

####################################################################################
#   JSON Utilities
####################################################################################

def read_json_file( f, empty, validate = False ):
    valid = True
    try:
        with open( f, 'r' ) as openfile:
            result = json.load( openfile )
            if validate:
                if type( empty ) != type( result ):
                    valid = False
                elif type( result ) == type( {} ):
                    if 'Format' not in result or result[ 'Format' ] != 'automagic':
                        valid = False
                elif type( result ) == type( [] ) and len( result ) >= 1:
                    valid = False
                    if 'ConfigInput' in result[0]:
                        for v in validate:
                            if v in result[0]['ConfigInput']:
                                valid = True
            if not valid:
                print( "File " + f + " was not written by this program, or is corrupt." )
                sys.exit()
            return result
    except IOError as e:
        if empty == 'fail':
            print( e )
            sys.exit( )
        return empty

def write_json_file( f, j ):
    try:
        with open( f, "w" ) as outfile:
            outfile.write( json.dumps( j, indent = 4 ) )
    except IOError as e:
        print( e )
        sys.exit( )

####################################################################################
#   Shelly-specific HTTP logic
####################################################################################

def status_url( address ):
    if dev_gen == 2:
        return "http://" + address + "/rpc/Sys.GetStatus"
    else:
        return "http://" + address + "/status"

def wifi_status_url( address ):
    return "http://" + address + "/rpc/Wifi.GetStatus"

def get_settings_url( address, rec = None ):
    if dev_gen == 2:
         #TODO-KBC - dst stuff?
         return "http://" + address + "/rpc/Sys.GetConfig"
    else:
         map = { "DeviceName" : "name", "LatLng" : "lat:lng", "TZ" : "tz_dst:tz_dst_auto:tz_utc_offset:tzautodetect" }
         parms = {}
         if rec:
              for tag in map:
                  if tag in rec:
                      for elem in zip( map[ tag ].split(':'), rec[ tag ].split(':') ):
                          parms[ elem[ 0 ]  ] = elem[ 1 ]
         q = "?" + url_encode( parms ) if parms else ""
         return "http://" + address + "/settings" + q

def ota_url( addr, fw ):
    if fw == 'LATEST':
        return "http://" + addr + "/ota?update=1"
    return "http://" + addr + "/ota?url=" + fw

def get_status( addr, tm, verbose ):
    url = status_url( addr )
    return get_url( addr, tm, verbose, url, 'to confirm status' )

def get_wifi_status( addr, tm, verbose ):
    url = wifi_status_url( addr )
    return get_url( addr, tm, verbose, url, 'to confirm status' )

def get_actions( addr, tm, verbose ):
    url = "http://" + addr + "/settings/actions"
    return get_url( addr, tm, verbose, url, 'to read actions' )

def get_toggle_url( ip, dev_type ):
    ### return "http://" + ip + "/" + dev_type + "/0?turn=on&timer=1"
    return "http://" + ip + "/" + dev_type + "/0?turn=toggle"

def toggle_device( ip_address, dev_type, verbosity = 0 ):
    success_cnt = 0
    fail_cnt = 0
    use_type = None
    while True:
        result = '' 
        # TODO: use dev_type to determine relay/light. For now, try both.
        for try_type in ( 'light', 'relay' ):
            if not use_type or use_type == try_type:
                url = get_toggle_url( ip_address, try_type )
                if verbosity > 2:
                    print( "Toggle url: '" + url + "'" )
                try:
                    result = url_read( url )
                    if result: use_type = try_type
                except BaseException as e:
                    if verbosity > 3:
                        eprint( "Error in toggle_device:", sys.exc_info( )[0] )
                    elif verbosity > 2:
                        if not any_timeout_reason( e ):
                            eprint( "Error in toggle_device:", sys.exc_info( )[0] )
                    result = ""
        if result != '':
            success_cnt += 1
            fail_cnt = 0
        else:
            fail_cnt += 1
        time.sleep( 0.5 )
        if success_cnt > 0 and fail_cnt > 1 or fail_cnt > 10: break

    if success_cnt == 0:
        print( "Unsuccessful attempt to toggle device." )

def ota_flash( addr, tm, fw, verbose, dry_run ):
    url = ota_url( addr, fw )
    if dry_run:
        print( url )
        return False
    return get_url( addr, tm, verbose, url, 'to flash firmware' )

def find_device( dev ):
    try:
        attempt = url_read( 'http://' + dev[ 'IP' ] + '/ota', tmout = 0.5 )
    except:
        attempt = None
    if attempt: return True
    return False

####################################################################################
#   Output Utilities
####################################################################################

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def fail_msg( s ):
    for i in range(3): print( )
    print( s )
    for i in range(3): print( )

####################################################################################
#   Library Functions
####################################################################################

def get_firmware_version( ota ):
    new_version = None
    try:
        contents = url_read( ota, 'b', tmout=60 )
    except:
        eprint( "Unexpected error [D]:", sys.exc_info( )[0] )
        contents = None
    if not contents or not zipfile.is_zipfile( stringtofile( contents ) ):
        print( "Could not fetch OTA firmware" )
        return
    zip = zipfile.ZipFile( stringtofile( contents ) )
    manifest = None
    for z in zip.namelist():
        if z.endswith('manifest.json'):
            manifest = z
            break
    if manifest:
        f = zip.open( manifest )
        contents = json.loads( f.read( ).decode('utf8') )
        new_version = contents[ 'build_id' ]
    return new_version

def check_for_device_queue( dq, group = None, include_complete = False, ssid = None, fail = True ):
    txt = " for SSID " + ssid if ssid else ""
    txt += ". Use import to specify some provisioning instructions."
    if len( dq ) == 0:
        print( "List is empty" + txt )
        sys.exit()
    for rec in dq:
        if 'ConfigInput' not in rec or 'ConfigStatus' not in rec: continue
        if ( include_complete or 'CompletedTime' not in rec['ConfigStatus'] ) and \
           ( not group or 'Group' in rec[ 'ConfigInput' ] and rec[ 'ConfigInput' ][ 'Group' ] == group ) and \
           ( not ssid or 'SSID' in rec[ 'ConfigInput' ] and rec[ 'ConfigInput' ][ 'SSID' ] == ssid ):
            return
    if group:
        print( "Group " + group + " has no list entries ready to provision" + txt )
    else:
        print( "List has no entries ready to provision" + txt )
    print( )
    if fail: sys.exit()

def short_heading( c ):
    return c if re.search( '\.[0-9]+\.', c ) else re.sub( '.*\.', '', c )

def get_name_value_pairs( query_conditions, term_type = '--query-condition' ):
    result = [ x.split('=') for x in query_conditions.split(',') ] if query_conditions else []
    for q in result:
        if len(q) < 2:
            print( "Each " + term_type + " term must contain name=value" )
            sys.exit()
    return result

def complete_probe( args, rec, initial_status = None ):
    global device_db

    ip_address = rec[ 'ConfigInput' ][ 'ProbeIP' ]
    if not args.refresh or args.operation == 'probe-list': eprint( ip_address )
    if not initial_status:
        initial_status = get_url( ip_address, args.pause_time, args.verbose, status_url( ip_address ), 'to get current status' )
    if initial_status:
        if args.verbose > 3:
            print( ip_address )
        configured_settings = get_url( ip_address, args.pause_time, args.verbose, get_settings_url( ip_address ), 'to get config' )
        actions = get_actions( ip_address, 1, args.verbose )
        if actions and 'actions' in actions:
            rec['actions'] = actions[ 'actions' ]
        id = initial_status['mac']
        if id in device_db:
            rec.update( device_db[ id ] )
        else:
            rec['ConfigStatus']['ProbeTime'] = time.time()
        rec['ConfigStatus']['UpdateTime'] = time.time()
        rec['status'] = initial_status
        if configured_settings:
            rec['settings'] = configured_settings
            rec['ConfigStatus']['CompletedTime'] = time.time()
        else:
            print( "Failed to update settings for " + id )
        rec['ConfigStatus']['Origin'] = 'probe-list'
        rec['Brand'] = 'Shelly'
        rec['IP'] = ip_address
        rec['ID'] = id
        device_db[ id ] = rec

def import_json( file, queue_file ):
    list = read_json_file( file, 'fail' )
    append_list( list )
    write_json_file( queue_file, device_queue )

def import_csv( file, queue_file ):
    with open( file ) as csvfile:
       reader = csv.DictReader( csvfile )
       append_list( reader )
    write_json_file( queue_file, device_queue )

def finish_up_device( device, rec, operation, args, new_version, initial_status, configured_settings, wifi_status ):
    global device_db
    rec[ 'ConfigStatus' ][ 'ConfirmedTime' ] = time.time()
    #need_update = False

    settings = get_name_value_pairs( args.settings, term_type = '--settings' )
    for pair in settings:
        if pair[0] in ( 'DeviceName', 'LatLng', 'TZ' ):
            if pair[0] not in rec:
                rec[ pair[0] ] = pair[1]

    rec[ 'ConfigStatus' ][ 'Origin' ] = operation
    rec[ 'Brand' ] = 'Shelly'
    rec[ 'ID' ] = initial_status[ 'mac' ]

    print( repr( initial_status ) )
    if wifi_status:
        rec[ 'IP' ] = wifi_status[ 'sta_ip' ]
    else:
        rec[ 'IP' ] = initial_status[ 'wifi_sta' ][ 'ip' ]

    rec = copy.deepcopy( rec )
    if not configured_settings: configured_settings = get_url( device, args.pause_time, args.verbose, get_settings_url( device, rec['ConfigInput'] ), 'to get config' )
    rec[ 'status' ] = initial_status
    if configured_settings and 'type' not in configured_settings[ 'device' ]: configured_settings[ 'device' ][ 'type' ] = rec[ 'ConfigStatus' ][ 'factory_ssid' ].split('-')[0]
    rec[ 'settings' ] = configured_settings if configured_settings else {}

    device_db[ initial_status[ 'mac' ] ] = rec
    write_json_file( args.device_db, device_db )

    #if 'DeviceName' in rec:
    #    new_settings = get_url( device, args.pause_time, args.verbose, get_settings_url( device, rec ), 'to set device name' )
    #    need_update = True
    #    rec['settings'] = new_settings

    disable_ap_mode( args, rec[ 'IP' ] )
    disable_BLE( args, rec[ 'IP' ] )

    if args.ota != '':
        if flash_device( device, args.pause_time, args.verbose, args.ota, args.ota_timeout, new_version, args.dry_run ):
    #        need_update = True
            new_status = get_status( device, args.pause_time, args.verbose )
            rec['status'] = new_status if new_status else {}
    #if need_update:
            device_db[ initial_status['mac'] ] = rec
            write_json_file( args.device_db, device_db )

    if args.print_using: 
        print_label( rec )

    if args.toggle:
        try:
            print( "Toggling power on newly provisioned device. Unplug to continue." )
            toggle_device( device, configured_settings['device']['type'] )
        except KeyboardInterrupt as error:
            print( )
            print( )

def read_device_queue( dq, args, ssid ):
    if args.operation == 'provision-list':
        for rec in device_queue:
            if 'ConfigInput' not in rec:
                continue
            cfg = rec[ 'ConfigInput' ]
            if 'ConfigStatus' in rec and 'CompletedTime' in rec['ConfigStatus'] or \
                args.group and ( not 'Group' in cfg or cfg[ 'Group' ] != args.group ) or \
                ssid and ssid != cfg[ 'SSID' ]:
                continue
            if 'ConfigStatus' not in rec: rec[ 'ConfigStatus' ] = {}
            rec[ 'ConfigStatus' ][ 'InProgressTime' ] = time.time()
            yield rec
    else:
        while True:
            rec = { 'SSID' : ssid }
            rec[ 'ConfigStatus' ] = {}
            rec[ 'ConfigStatus' ][ 'InProgressTime' ] = time.time()
            rec[ 'ConfigInput' ] = {}
            rec[ 'ConfigInput' ][ 'SSID' ] = ssid
            yield rec

def prompt_to_continue( ):
    print()
    print()
    getpass.getpass( "Press <enter> to continue" )

####################################################################################
#   Query/db functions
####################################################################################

def flatten( d, prefix='', devtype = None ):
    result = { }
    guide = { }
    if not devtype and type(d) == type({}):
        devtype = d['settings']['device']['type'] if 'settings' in d and 'device' in d['settings'] and 'type' in d['settings']['device'] else None
    if type( d ) == type( { } ):
        for k in d:
            new_data = {}
            new_guide = {}
            if type( d[k] ) == type( {} ):
                ( new_data, new_guide ) = flatten( d[k], prefix + k + '.', devtype )
            elif type( d[k] ) == type( [] ):
                for i in range(len(d[k])):
                    ( tmp_data, tmp_guide ) = flatten( d[k][i], prefix + k + '.' + str(i) + '.', devtype )
                    new_data.update( tmp_data )
                    new_guide.update( tmp_guide )
            else:
                new_data[ k ] = str( d[k] )
                new_data[ prefix + k ] = str( d[k] )
                pk = re.sub( '\.[0-9]+\.', '.[n].', prefix + k )
                if k not in new_guide: new_guide[ k ] = { }
                new_guide[ k ][ devtype ] = pk
                if prefix + k not in new_guide: new_guide[ prefix + k ] = { }
                new_guide[ prefix + k ][ devtype ] = pk
            new_data.update( result )       # Give first/original key priority
            result = new_data               # and replace result for next iteration
            guide = deep_update( new_guide, guide )
    else:
        guide = {}
        pk = re.sub( '\.[0-9]+\.', '.[n].', prefix )
        pk = re.sub( '\.$', '', pk )
        prefix = re.sub( '\.$', '', prefix )
        if prefix not in guide: guide[ prefix ] = { }
        guide[ prefix ][ devtype ] = pk
        result = { prefix : str( d ) }
    return [ result, guide ]

def match_rec( rec, query_conditions, match_tag, group, restore_device, access ):
    for q in query_conditions:
        if q[0] not in rec or rec[q[0]] != q[1]:
            return False
    if match_tag and ( 'Tags' not in rec or match_tag not in rec['Tags'].split(',') ):
        return False
    if group and ( not 'Group' in rec or rec[ 'Group' ] != group ):
        return False
    if restore_device and restore_device != 'ALL' and restore_device != rec[ 'ID' ]:
        return False
    if access != 'ALL':
        presumed = 'Continuous' if 'Access' not in rec else rec[ 'Access' ]
        if presumed != access:
            return False
    return True

def schema_details( col, coverage, verbosity ):
    paths = {}
    for devtype in coverage:
        if coverage[devtype] not in paths:
            paths[ coverage[devtype] ] = []
        paths[coverage[devtype]].append( devtype )
    if verbosity > 1:
        return [ col, paths ]
    elif verbosity > 0:
        if len(paths) > 1 or list(paths.keys())[0] != re.sub('\.[0-9]+\.','.[n].',col):
            return [ col, paths ]
        else:
            return [ col ]
    else:
        return [ col ]

def print_details( col, paths, verbosity, max_width ):
    if verbosity > 1 and paths:
        print( col + ':' )
        for p in paths:
            #print(repr(p))
            #print(repr(paths))
            ###print( '    ' + p  + ' [ '+  ', '.join( paths[ p ] ) + ' ]' )
            print( '    ' + p  + ' [ '+  ', '.join( [ x for x in paths[ p ] if x ] ) + ' ]' )
    elif verbosity > 0 and paths:
        print( col.ljust( max_width ) + ': ' + ', '.join( p for p in paths ) )
    else:
        print( col )

####################################################################################
#   Operations
####################################################################################

def flash_device( addr, pause_time, verbose, ota, ota_timeout, new_version, dry_run ):
    global ota_version_cache
    print( "Checking old firmware version" )
    url = "http://" + addr + "/ota"
    result = get_url( addr, pause_time, verbose, url, 'to get current version' )
    if not result:
        print( "Could not get current firmware version for device " + addr )
        return False
    print( "status: " + result['status'] + ", version: " + result['old_version'] )
    if not new_version and ota == 'LATEST':
        settings = get_url( addr, pause_time, verbose, get_settings_url( addr ), 'to get current device type' )
        if not settings:
            print( "Could not get settings for device " + addr )
            return False
        dev_type = settings[ 'device' ][ 'type' ]
        if not ota_version_cache:
            ota_version_cache = settings = get_url( 'api.shelly.cloud', pause_time, verbose, 'https://api.shelly.cloud/files/firmware', 'to get current device type' )
        new_version = ota_version_cache[ 'data' ][ dev_type ][ 'version' ]
    if result['old_version'] == new_version:
        print( "Device is already up-to-date" )
        return True
    if result['status'] == 'updating':
        print( 'Error: Device already shows "updating"' )
        return False
    if ota_flash( addr, pause_time, ota, verbose, dry_run ):
        print( "Sent OTA instructions to " + addr )
        print( "New version: " + new_version )
        if ota_timeout == 0: return True
        print( "Pausing to wait to check for successful OTA flash..." )
        start_time = time.time( )
        seen_updating = False
        passes = 0
        while time.time( ) < start_time + ota_timeout:
            passes += 1
            time.sleep( pause_time )
            new_result = get_url( addr, pause_time, verbose, url, '' )
            if not new_result: return False
            if new_result['status'] == 'updating': 
                if seen_updating:
                    print( '.', end='' )
                    sys.stdout.flush( )
                else:
                    print( "status: " + new_result['status'] + ", version: " + new_result['old_version'] )
                seen_updating = True
            elif seen_updating:
                if not new_version or result['old_version'] != new_result['old_version']:
                    if not new_version or new_result['old_version'] == new_version:
                        print( "" )
                        print( "Success. Device " + addr + " updated from " + result['old_version'] + ' to ' + new_result['old_version'] )
                        return True
                    else:
                        print(repr(new_version))
                        fail_msg( "****possible OTA failure***  Device " + addr + ' still has unexpected build, not matching manifest: ' + new_result['old_version'] )
                        return False
                else:
                    break
            else:
                if passes > 10:
                    print( 'The device ' + addr + ' has never shown status "updating". Is it already on the version requested? ' )
                    return False
                print( "status: " + new_result['status'] + ", version: " + new_result['old_version'] )
        fail_msg( "****possible OTA failure***  Device " + addr + ' still has ' + new_result['old_version'] )
        return False

    else:
        if not dry_run: print( "Could not flash firmware to device " + addr )
        return False
    return True

def schema( args ):
    query_columns = args.query_columns.split(',') if args.query_columns else []
    query_conditions = get_name_value_pairs( args.query_conditions )
    u = {}
    guide = {}

    if args.refresh: probe_list( args )

    for d in device_db:
        if d == 'Format':
            continue
        ( data, new_guide ) = flatten( device_db[ d ] )
        if match_rec( data, query_conditions, args.match_tag, args.group, None, args.access ):
            u.update( data )
            guide = deep_update( new_guide, guide )
    k = sorted( u.keys() )
    max_width = 0
    for s in k:
        if len(s) > max_width:
            max_width = len(s)
    results = []
    for s in k:
        if len( query_columns ) > 0:
            for q in query_columns:
                if q.split( '.' )[-1] in s.split( '.' ) and q in s:
                    results.append( schema_details( s, guide[ s ], args.verbose ) )
        else:
            results.append( schema_details( s, guide[ s ], args.verbose ) )

    for z in ( None, 'settings.', 'status.', '' ):
        for s in results:
            path = list(s[1].keys())[0] if len(s) > 1 else None
            if not z and z != '' and not path  \
                or z and z != '' and path and path.startswith(z) \
                or z and z == '' and path and not ( path.startswith('settings.') or path.startswith('status.') ):
                   print_details( s[0], s[1] if len(s) > 1 else None, args.verbose, max_width )

def apply( args, new_version, data, need_write ):
    configured_settings = None

    if args.ota != '':
        flash_device( data[ 'IP' ], args.pause_time, args.verbose, args.ota, args.ota_timeout, new_version, args.dry_run )

    if args.apply_urls:
        for url in args.apply_urls:
            if args.dry_run:
                print( 'http://' + data[ 'IP' ] + '/' + url )
            else:
                got = get_url( data[ 'IP' ], args.pause_time, args.verbose, 'http://' + data[ 'IP' ] + '/' + url, 'to apply /' + url )
                if not args.settings:
                    configured_settings = get_url( data[ 'IP' ], args.pause_time, args.verbose, get_settings_url( data[ 'IP' ] ), 'to get config' )
                    need_write = True

    if args.settings:
        new_settings = dict( get_name_value_pairs( args.settings, term_type = '--settings' ) )
        if args.dry_run:
            print( get_settings_url( data[ 'IP' ], new_settings ) )
        else:
            # apply_urls (above) depends on this, if both are used (to save time):
            configured_settings = get_url( data[ 'IP' ], args.pause_time, args.verbose, get_settings_url( data[ 'IP' ], new_settings ), 'to get config' )
            need_write = True

    if args.delete_device and ( args.delete_device == 'ALL' or args.delete_device == data[ 'ID' ] ):
        del device_db[ data[ 'ID' ] ]
        need_write = True

    if args.restore_device and ( args.restore_device == 'ALL' or args.restore_device == data[ 'ID' ] ):
        settings = device_db[ data[ 'ID' ] ][ 'settings' ]
        http_args = {}
        apply_list = []

        for s in settings:
            fields = settings[ s ]
            if type( fields ) == type( {} ):
                if s in exclude_setting: continue
                s = re.sub( '^wifi_', '', s )
                if s == 'sntp':
                    apply_list.append( 'http://' + data[ 'IP' ] + '/settings/?' + url_encode( { 'sntp_server' : '' if fields['enabled'] == 'false' else fields[ 'server' ] } ) )
                elif s == 'mqtt':
                    flds = dict( [ [ 'mqtt_' + f, fields[ f ] ] for f in fields ] )
                    apply_list.append( 'http://' + data[ 'IP' ] + '/settings/?' + url_encode( flds ) )
                elif s == 'coiot':
                    flds = dict( [ [ 'coiot_' + f, fields[ f ] ] for f in fields ] )
                    apply_list.append( 'http://' + data[ 'IP' ] + '/settings/?' + url_encode( flds ) )
                else:
                    apply_list.append( 'http://' + data[ 'IP' ] + '/settings/' + s + '?' + url_encode( fields ) )
            elif type( fields ) == type( [] ):
                if s in exclude_setting: continue
                name = re.sub( 's$', '', s )
                for i in range( len( fields ) ):
                    channel = fields[ i ]
                    if type( channel ) == type( {} ):
                        flds = dict( [ [ f, channel[ f ] ] for f in channel if f not in exclude_setting ] )
                        apply_list.append( 'http://' + data[ 'IP' ] + '/settings/' + name + '/' + str( i ) + '/?' + url_encode( flds ) )
                        if 'schedule' in channel and 'schedule_rules' in channel:
                             rules = ','.join( channel[ 'schedule_rules' ] )
                             apply_list.append( 'http://' + data[ 'IP' ] + '/settings/' + name + '/' + str( i ) + '/?' + \
                                url_encode( { 'schedule' : channel[ 'schedule' ], 'schedule_rules': rules } ).replace("schedule_rules=%5B%5D","").replace("%5B","[").replace("%5D","]" ) )
                    ### else:  'alt_modes' : 'white' ???
            elif s not in exclude_setting:
                http_args[ s ] = fields

        apply_list.append( 'http://' + data[ 'IP' ] + '/settings?' + url_encode( http_args ) )
                
        if 'actions' in device_db[ data[ 'ID' ] ]:
            actions = device_db[ data[ 'ID' ] ][ 'actions' ]
            for a in actions:
                for u in actions[ a ]:
                    ### btn_on_url: [{u'index': 0, u'enabled': True, u'urls': [u'http://192.168.1.254/zzzfoo']}]
                    http_args = [ ( 'urls[]', x ) for x in u[ 'urls' ] ]
                    http_args.append( ( 'name', a ) )
                    for p in u:
                        if p != 'urls':
                            http_args.append( ( p, u[ p ] ) )
                    apply_list.append( 'http://' + data[ 'IP' ] + '/settings/actions?' + url_encode( http_args ) )

        if args.dry_run:
             for u in apply_list:
                   print(u)
             print( )
        else:
             for u in apply_list:
                 got = get_url( data[ 'IP' ], args.pause_time, args.verbose, u, None)
                 if not got:
                     print( "could not apply " + u )
                 time.sleep( .1 )
                 sys.stdout.write( "." )
                 sys.stdout.flush()
             print( )
             configured_settings = get_url( data[ 'IP' ], args.pause_time, args.verbose, get_settings_url( data[ 'IP' ] ), 'to get config' )
             need_write = 1

    return( configured_settings, need_write )

def query( args, new_version = None ):
    global device_db
    need_write = False
    results = [ ]

    if args.refresh: probe_list( args )

    if args.query_columns and args.query_columns.startswith('+'):
        query_columns = default_query_columns
        query_columns.extend( re.sub('^\+','',args.query_columns).split(',') )
    else:
        query_columns = args.query_columns.split(',') if args.query_columns else default_query_columns

    delete_list = [ q.replace("-","") for q in query_columns if q.startswith("-") ]
    query_columns = [ q for q in query_columns if q not in delete_list and not q.startswith("-") ]

    query_conditions = get_name_value_pairs( args.query_conditions )

    guide = {}
    tmp = []
    for d in device_db:
        if d == 'Format':
            continue
        ( data, new_guide ) = flatten( device_db[ d ] )
        guide.update( new_guide )
        tmp.append( data )

    column_map = {}
    for q in query_columns:
        if q not in guide:
            for g in guide:
                if g.endswith( "." + q ):
                     column_map[ q ] = g

    query_columns = [ column_map[ q ] if q in column_map else q for q in query_columns ]

    column_widths = { }
    for data in tmp:
        res = { }
        for c in query_columns:
            hc = c if args.verbose > 0 else short_heading( c ) 
            res[ c ] = data[ c ] if c in data else ""
            if hc not in column_widths: column_widths[ hc ] = len( hc )
            column_widths[ hc ] = len( res[ c ] ) if len( res[ c ] ) > column_widths[ hc ] else column_widths[ hc ]
        results.append( [ res, data ] )

    res = ""
    dashes = ""
    for c in query_columns:
        hc = c if args.verbose > 0 else short_heading( c )
        res = res + hc.ljust( column_widths[ hc ]+1 )
        dashes = dashes + ( "-" * column_widths[ hc ] ) + " "
    print( res )
    print( dashes )

    k = list( results[0][0].keys( ) )[0]
    results.sort( key=lambda x: x[0][k], reverse=False )
    todo = []
    nogo = []
    continuous_count = 0
    total_count = 0
    for ( res, data ) in results:
        if match_rec( data, query_conditions, args.match_tag, args.group, args.restore_device, args.access ):
            if args.set_tag or args.delete_tag:
                need_write = True
                old_tags = data['Tags'].split(',') if 'Tags' in data and data['Tags'] != '' else []
                if args.set_tag:
                    data[ 'Tags' ] = ','.join( set( old_tags ).union( [ args.set_tag ] ) )
                if args.delete_tag:
                    data[ 'Tags' ] = ','.join( set( old_tags ).difference( [ args.delete_tag ] ) )
                device_db[ data[ 'ID' ] ][ 'Tags' ] = data[ 'Tags' ]
            result = ""
            for c in query_columns:
                hc = c if args.verbose > 0 else short_heading( c )
                result += res[ c ].ljust( column_widths[ hc ]+1 )

            if 'IP' not in data:
                nogo.append( [ result, res, data ] )
            else:
                if 'Access' not in data or data[ 'Access' ] == 'Continuous':
                    continuous_count += 1
                todo.append( [ result, res, data ] )
                total_count += 1

    for (result, res, data) in todo:
         print( result )

    if args.operation == 'apply':
        print( )
        if nogo:
            print( "These devices have no stored IP. No changes will be applied." )
            for (result, res, data) in nogo:
                print( data[ 'ID' ] )
            print( )

        if todo and not args.dry_run:
            print( "Applying changes..." )

        done = []
        passes = 0
        configured_settings = None
        while( todo ):
            passes += 1
            for ( result, res, data ) in todo:
                if find_device( data ):
                    done.append( [ result, res, data ] )
                    if not args.dry_run: print( data[ 'IP' ] )
                    ( configured_settings, need_write ) = apply( args, new_version, data, need_write )
                    total_count -= 1
                    if 'Access' not in data or data[ 'Access' ] == 'Continuous':
                        continuous_count -= 1
                        
                        if continuous_count == 0 and total_count > 0:
                           print( )
                           print( "Only Periodic WiFi-connected devices remain. Polling until they are found..." )

            todo = [ r for r in todo if r not in done ]

            if passes > 10 and continuous_count == total_count:
                 print( )
                 print( "These device could not be contacted:" )
                 for ( result, res, data ) in todo:
                    print( data[ 'IP' ] )
                 break

            if todo:
                time.sleep(.5)

        if configured_settings:
            device_db[ data[ 'ID' ] ][ 'settings' ] = configured_settings

    if need_write:
        write_json_file( args.device_db, device_db )

def probe_list( args ):
    query_conditions = [ x.split('=') for x in args.query_conditions.split(',') ] if args.query_conditions else []

    if args.refresh:
        dq = [ device_db[ k ] for k in device_db.keys() if k != 'Format' and 'ProbeIP' in device_db[ k ][ 'ConfigInput' ] ]
    else:
        dq = device_queue

    todo = []
    for rec in dq:
        if match_rec( rec, query_conditions, args.match_tag, args.group, None, args.access ) and 'ProbeIP' in rec['ConfigInput']: 
            todo.append( rec )

    if not args.refresh:
        check_for_device_queue( todo, args.group, True )

    if args.refresh and args.operation != 'probe-list':
        eprint( "Refreshing info from network devices" )

    done = []
    probe_count = 1
    need_write = False
    # todo: Look for Periodic-type devices in queue and message that it might take a while
    while len( todo ):
        if args.operation == 'probe-list' and not args.verbose:
            sys.stdout.write( "." )
            sys.stdout.flush()
        for rec in todo:
            cfg = rec[ 'ConfigInput' ]
            if need_write and ( probe_count % 10 == 0 or 'Access' in cfg and cfg[ 'Access' ] == 'Periodic' ):
                write_json_file( args.device_db, device_db )
            initial_status = None
            try:
                initial_status = json.loads( url_read( status_url( cfg[ 'ProbeIP' ] ), tmout = 0.5 ) )
                break
            except BaseException as e:
                if any_timeout_reason( e ):
                    pass
                else:
                    eprint( "Unexpected error [A]:", str( e.reason ) )  ### sys.exc_info( )[0] )
                    sys.exit()
        if initial_status:
             done.append( rec )
             complete_probe( args, rec, initial_status )
             need_write = True
             probe_count += 1
        time.sleep( 0.5 )
        todo = [ r for r in todo if r not in done ]

    if need_write:
        write_json_file( args.device_db, device_db )

def acceptance_test( args, credentials ):
    prior_ssids = {}
    while True:
        found = wifi_connect( credentials, args.prefix, prefix=True, ignore_ssids=prior_ssids, verbose=args.verbose )
        if found:
            prior_ssids[ found ] = 1
            print( "Found " + found )
            print( "Will attempt to toggle relay." )
            time.sleep( 1 )
            print( "When you hear it click, you can unplug the device to try another" )
            toggle_device( "192.168.33.1", None, args.verbose )
            print( )
            print( "Searching for another device" )
        else:
            print( "Failed to find device to test. Pausing for 15s to try again" )
            time.sleep( 15 )

def check_status( ip_address, devname, ssid, rec, new_version, args ):
    initial_status = get_status( ip_address, args.pause_time, args.verbose )
    wifi_status = None

    if initial_status:
        print( "Confirmed device " + devname + " on " + ssid + ' network' )
        if args.operation == 'config-test':
            print( "Toggling device.  Use ^C (control-C) to continue" )
            try:
                toggle_device( ip_address, None )
            except:
                pass
            factory_reset( ip_address, None )
            answer = input( 'Remove device and connect another. Continue?' )
            if answer and answer.upper() not in ('Y','YES'):
                return 'Quit'
            return initial_status
        else:
            if dev_gen == 2:
                wifi_status = get_wifi_status( ip_address, args.pause_time, args.verbose )
            finish_up_device( ip_address, rec, args.operation, args, new_version, initial_status, None, wifi_status )
            return initial_status
    print( "Could not find device on " + ssid + ' network' )
    return "Fail"

def provision_device( addr, tries, args, ssid, pw, cfg ):
    got_one = False
    print( "Sending network credentials to device" )
    static_ip = cfg[ 'StaticIP' ] if 'StaticIP' in cfg and args.operation == 'provision-list' else None
    netmask = cfg[ 'NetMask' ] if 'StaticIP' in cfg and args.operation == 'provision-list' else None
    gateway = cfg[ 'Gateway' ] if 'StaticIP' in cfg and args.operation == 'provision-list' else None

    for i in range(5):
        time.sleep( args.pause_time )
        # Load the URL multiple(?) times, even if we are successful the first time
        for j in range(tries):
            try:
                if dev_gen == 2:
                    ( req, data ) = set_wifi_post( addr, ssid, pw, static_ip, netmask, gateway )
                    res = rpc_post( req, data )
                    content = json.loads( res )
                else:
                    req = set_wifi_get( addr, ssid, pw, static_ip, netmask, gateway )
                    content = json.loads( url_read( req ) )
                if 'error' in content or args.verbose > 2:
                    print( repr( [req, data] ) )
                    print( repr( content ) )
                if 'error' not in content:
                    got_one = True
            except:
                if not got_one: eprint( "Unexpected error [B]:", sys.exc_info( )[0] )
        if got_one: return True
    print( "Tried multiple times and could not instruct device to set up network" )
    return False

def gen2_rpc( verbosity, txn ):
    ( req, data ) = txn
    res = rpc_post( req, data )
    content = json.loads( res )
    if verbosity > 2:
        print( repr( [req, data] ) )
        print( repr( content ) )

def disable_ap_mode( args, addr ):
    if dev_gen == 2:
        gen2_rpc( args.verbose, disable_ap_post( addr ) )

def disable_BLE( args, addr ):
    if dev_gen == 2:
        gen2_rpc( args.verbose, disable_BLE_post( addr ) )

def provision_native( credentials, args, new_version ):
    global device_queue, device_db, dev_gen
    t1 = timeit.default_timer()

    prior_ssids = {}
    ssid = credentials['ssid']
    pw = credentials['password']

    if args.ssid and args.ssid != ssid:
        print('Connect to ' + args.ssid + ' first')
        sys.exit()

    if not args.ssid:
        print( "Found current SSID " + ssid + ". Please be sure this is a 2.4GHz network before proceeding." )

    if args.operation == 'provision-list':
        check_for_device_queue( device_queue, args.group, ssid = ssid )

    if not args.ssid:
        answer = input( 'Connect devices to SSID ' + ssid + '? (Y/N)> ' )
        if answer.upper() not in ('Y','YES'):
            print('Connect to desired SSID first')
            sys.exit()
    init()
    setup_count = 0
    success_count = 0
    for rec in read_device_queue( device_queue, args, ssid ):
        cfg = rec[ 'ConfigInput' ]
        if setup_count > 0 and args.cue:
            prompt_to_continue()
        setup_count += 1
        print( "Waiting to discover a new device" )

        init()
        t1 = timeit.default_timer()
        found = wifi_connect( credentials, args.prefix, prefix=True, ignore_ssids=prior_ssids, verbose=args.verbose )
        if found:
            dev_gen = 2 if "Plus" in found else 1
            if args.timing: print( 'discover time: ', round( timeit.default_timer() - t1, 2 ) )
            print( "Ready to provision " + found + " with " + repr( cfg ) )
            prior_ssids[ found ] = 1
            time.sleep( args.pause_time )
            if not get_status( factory_device_addr, args.pause_time, args.verbose ):
                print( "Failed to contact device after connecting to its AP" )
                if not wifi_reconnect( credentials ):
                    print( "Could not reconnect to " + ssid )
                break

            rec[ 'ConfigStatus' ][ 'factory_ssid' ] = found
            write_json_file( args.device_queue, device_queue )

            setup_count += 1
            stat = provision_device( factory_device_addr, 3, args, ssid, pw, cfg )

            ### Connect (back) to main network
            if not wifi_reconnect( credentials ):
                print( "Could not reconnect to " + ssid )
                break

            if not stat:
                sys.exit(0)

            rec[ 'ConfigStatus' ][ 'CompletedTime' ] = time.time()
            success_count += 1
            if args.operation != 'config-test':
                 write_json_file( args.device_queue, device_queue )

            if 'StaticIP' in cfg:
                ip_address = cfg[ 'StaticIP' ]
            else:
                ip_address = found

            print( "Attempting to connect to device back on " + ssid )
            stat = check_status( ip_address, found, ssid, rec, new_version, args )
            if stat == 'Quit': return false
            if stat == 'Fail': break

            disable_ap_mode( args, ip_address )
        else:
            if args.wait_time == 0:
                print("Exiting. No additional devices found and time-to-wait is 0. Set non-zero time-to-wait to poll for multiple devices.")
                break
            if args.verbose > 0:
                print( 'Found no new devices. Waiting ' + str(args.wait_time) + ' seconds before looking again. Press ^C to cancel' )
            time.sleep( args.wait_time )

def provision_ddwrt( args, new_version ):
    global device_queue, device_db, dev_gen
    t1 = timeit.default_timer()
    check_for_device_queue( device_queue, args.group )
    ( ap_node, sta_node ) = ddwrt_choose_roles( args.ddwrt_name )
    if args.timing: print( 'setup time: ', round( timeit.default_timer() - t1, 2 ) )
    setup_count = 0
    success_count = 0
    for rec in read_device_queue( device_queue, args, None ):
        cfg = rec[ 'ConfigInput' ]
        if setup_count > 0 and args.cue:
            prompt_to_continue()
        setup_count += 1
        sys.stdout.write( "Waiting to discover a new device" )
        while True:
            sys.stdout.write( "." )
            sys.stdout.flush()
            t1 = timeit.default_timer()
            device_ssids = ddwrt_discover( sta_node, args.prefix )
            if len( device_ssids ) > 0:
                print( "" )
                dev_gen = 2 if "Plus" in device_ssids[0] else 1
                if args.timing: print( 'discover time: ', round( timeit.default_timer() - t1, 2 ) )
                print( "Ready to provision " + device_ssids[0] + " with " + repr( cfg ) )
                rec[ 'ConfigStatus' ][ 'factory_ssid' ] = device_ssids[0]
                rec[ 'ConfigStatus' ][ 'InProgressTime' ] = time.time()
                write_json_file( args.device_queue, device_queue )

                attempts = 0
                while True:
                    attempts += 1
                    # With different ddwrt devices, faster to pre-configure AP
                    t1 = timeit.default_timer()
                    if ap_node[ 'router' ][ 'et0macaddr' ] != sta_node[ 'router' ][ 'et0macaddr' ]:
                        ddwrt_set_ap_mode( ap_node, cfg[ 'SSID' ], cfg[ 'Password' ] )

                    # do this each time, assuming ssid changes... optimization possible if recognize repeated SSIDs(?)
                    ddwrt_set_sta_mode( sta_node, device_ssids[0] )
                    if args.timing: print( 'dd-wrt device configuration time: ', round( timeit.default_timer() - t1, 2 ) )

                    t1 = timeit.default_timer()
                    ddwrt_ssh_loopback( sta_node )
                    if args.timing: print( 'setting ssh loopback time: ', round( timeit.default_timer() - t1, 2 ) )
                    forwarded_addr = sta_node['router']['address'] + ':8001'

                    t1 = timeit.default_timer()
                    if not get_status( forwarded_addr, args.pause_time, args.verbose ):
                        print( "Failed to contact device after connecting to its AP" )
                        time.sleep(600)
                        sys.exit( )

                    t1 = timeit.default_timer()
                    # try just once if using a single DDWRT device, because of timing... need to reconfigure quickly
                    tries = 1 if ap_node[ 'router' ][ 'et0macaddr' ] == sta_node[ 'router' ][ 'et0macaddr' ] else 3
                    stat = provision_device( forwarded_addr, tries, args, cfg[ 'SSID' ], cfg[ 'Password' ], cfg )
                    if args.timing: print( 'settings time: ', round( timeit.default_timer() - t1, 2 ) )
                    if stat: break
                    
                    if attempts >= 10:
                        print( "Device failed to take WiFi provisioning instructions after 10 attempts." )
                        sys.exit( )
                    else:
                        print( "Device failed to take WiFi provisioning instructions. Trying again." )
                        next

                # If just one ddwrt device, then switch from sta back to AP now
                if ap_node[ 'router' ][ 'et0macaddr' ] == sta_node[ 'router' ][ 'et0macaddr' ]:
                    t1 = timeit.default_timer()
                    ddwrt_set_ap_mode( ap_node, cfg[ 'SSID' ], cfg[ 'Password' ] )
                    if args.timing: print( 'dd-wrt device reconfig time: ', round( timeit.default_timer() - t1, 2 ) )

                t1 = timeit.default_timer()
                if 'StaticIP' in cfg:
                    ip_address = cfg[ 'StaticIP' ]
                else:
                    ip_address = device_ssids[ 0 ]

                print( "Attempting to connect to device on " + cfg['SSID'] )
                new_status = check_status( ip_address, device_ssids[ 0 ], cfg['SSID'], rec, new_version, args )
                if new_status == 'Quit': return False
                if new_status == 'Fail':
                    print( "Failed to find device on network" )
                    sys.exit()

                if args.timing: print( 'WiFi transition time:', round( timeit.default_timer() - t1, 2 ) )

                success_count += 1
                rec[ 'ConfigStatus' ][ 'CompletedTime' ] = time.time()
                write_json_file( args.device_queue, device_queue )

                if args.verbose > 2: print( repr( new_status ) )
                print( )

                ### this moved to check_status()
                ##wifi_status = None
                ##if dev_gen == 2:
                ##    wifi_status = get_wifi_status( ip_address, args.pause_time, args.verbose )
                ##finish_up_device( ip_address, rec, args.operation, args, new_version, new_status, None, wifi_status )

                break
            else:
                ## print( 'Found no new devices. Waiting ' + str(args.wait_time) + ' seconds before looking again. Press ^C to cancel' )
                time.sleep( args.wait_time )
    print( "Successfully provisioned " + str( success_count ) + " out of " + str( setup_count ) + " devices." )

def append_list( l ):
    global device_queue
    n = 0
    for row in l:
        n += 1
        r = {}
        for k in required_keys:
            if k not in row and 'ProbeIP' not in row:
                print( 'Required key ' + k + ' missing at record ' + str( n ) + ' of import file' )
                print( repr( row ) )
                sys.exit()
            if k in row:
                r[ k ] = row[ k ]
        for k in optional_keys:
            if k in row:
                if k == 'StaticIP' and row[ k ]:
                    r[ 'IP' ] = row[ 'StaticIP' ]
                    if 'NetMask' not in row or not row[ 'NetMask' ]:
                        print( "Record " + str( n ) + " contains StaticIP but not NetMask. Correct the import file to supply both." )
                        sys.exit( )
                if k == 'LatLng' and row[ k ]:
                    if not re.match('^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+):[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$',row[ k ]):
                        print( "Record " + str( n ) + " contains improper LatLng. Must be of the form lat:lng" )
                        sys.exit( )
                if k == 'TZ' and row[ k ]:
                    if not re.match('^(True|False):(True|False):[+-]?([0-9]+):(True|False)$',row[ k ]):
                        print( "Record " + str( n ) + " contains improper TZ. Must be of the form tz_dst:tz_dst_auto:tz_utc_offset:tzautodetect" )
                        sys.exit( )

                if k == 'Access' and row[ k ]:
                    if row[ k ] not in ( 'Continuous', 'Periodic' ):
                        print( "Record " + str( n ) + " contains improper Access value. Must be one of Continuous or Periodic" )
                        sys.exit( )

                r[ k ] = row[ k ]
        if 'ProbeIP' in r: r['ProbeIP'] = r['ProbeIP'].strip()
        t = { 'ConfigInput' : r, 'ConfigStatus' : { 'InsertTime' : time.time() } }
        device_queue.append( t )

def print_list( queue_file, group ):
    check_for_device_queue( device_queue, group, fail=False )

    print( "List of devices for probe-list or provision-list operation" )
    header = [ 'ProbeIP', 'Group', 'SSID', 'Password', 'StaticIP', 'NetMask', 'Gateway', 'DeviceName', 'InsertTime', 'CompletedTime' ]
    col_widths = [ 0 ] * len(header)
    result = [ header, [] ]
    for d in device_queue:
        if 'Group' in d['ConfigInput'] and d['Group']['ConfigInput'] == group or not group:
            rec = []
            for h in header:
                rec.append( d['ConfigInput'][h] if h in d['ConfigInput'] else d['ConfigStatus'][h] if h in d['ConfigStatus'] else '' )
            result.append( rec )
            for i in range( len( header ) ):
                if len( str( rec[i] ) ) > col_widths[i]:
                    col_widths[i] = len( str( rec[i] ) )
    for i in range(len(header)):
        if col_widths[i] > 0 and col_widths[i] < len(header[i]):
            col_widths[i] = len(header[i])
    for rec in result:
        pr = ""
        if len(rec):
            for v in zip( rec, col_widths ):
                 if v[1] > 0:
                     pr += str( v[0] ).ljust( v[1] + 1 )
        else:
            for i in range(len(header)):
                 if col_widths[i] > 0:
                     pr += "-" * col_widths[i] + " "
        print( pr )

def clear_list( queue_file ):
    write_json_file( queue_file, [] )

def identify( device_address ):
    toggle_device( device_address, None )

def myfunc( e ): 
    return repr( e[ 'version' ].split('.') )

def list_versions( addr, pause_time, verbose ):
    settings = get_url( addr, pause_time, verbose, get_settings_url( addr ), 'to get current device type' )
    if not settings:
        print( "Could not get settings for device " + addr )
        return
    dev_type = settings[ 'device' ][ 'type' ]

    url = "http://archive.shelly-tools.de/archive.php?type=" + dev_type
    versions = get_url( 'archive.shelly-tools.de' , pause_time, verbose, url, 'to get firmware list' )
    hwidth = 0
    for v in versions:
        if len( v[ 'version' ] ) > hwidth: hwidth = len( v[ 'version' ] )

    versions.sort( key=lambda e : [ int( re.sub( r'[^0-9]', '', k ) ) for k in e[ 'version' ].split('.') ] )

    if verbose > 1:
        firmware_db = read_json_file( 'shelly-fw-versions.json', {}, True )
        firmware_db[ 'Format' ] = 'automagic'
    for v in versions:
        url = "http://archive.shelly-tools.de/version/" + v[ 'version' ] + "/" + v[ 'file' ]
        print( v[ 'version' ].ljust( hwidth ) + "    " + ( "http://" + addr + "/ota?url=" if verbose else "" ) + url )
        if verbose > 1:
            if url not in firmware_db:
                new_version = get_firmware_version( url )
                firmware_db[ url ] = new_version
            print( "    " + firmware_db[ url ] )
    if verbose > 1:
        write_json_file( 'shelly-fw-versions.json', firmware_db )

def replace_device( db_path, from_device, to_device ):
    global device_db

    for d in [ ('from', from_device ), ('to', to_device ) ]:
        if d[1] not in device_db:
            print( "--" + d[0] + " device " + d[1] + " is not stored in the device db " + db_path )
            sys.exit()

    saved = copy.deepcopy( device_db[ to_device ][ 'settings' ] )

    device_db[ to_device ][ 'settings' ] = copy.deepcopy( device_db[ from_device ][ 'settings' ] )
    for n in exclude_from_copy:
        src = saved
        dest = device_db[ to_device ][ 'settings' ]
        for k in n.split('.'):
            if k in src:
                src = src[ k ]
                if type( src ) == type( {} ):
                     if not k in dest:
                         dest[ k ] = {}
                     dest = dest[ k ]
                else:
                     dest[ k ] = src

    device_db[ to_device ][ 'actions' ] = device_db[ from_device ][ 'actions' ]
    write_json_file( db_path, device_db )

def factory_reset( device_address, verbose ):
    try:
        contents = json.loads( url_read( "http://" + device_address + "/settings/?reset=1" ) )
        if verbose > 2:
            print( repr( contents ) )
        print( "Reset sent to " + device_address )
    except BaseException as e:
        print( "Reset failed" )
        if any_timeout_reason( e ):
            print( "Device is not reachable on your network" )
            return
        print( "Unexpected error [C]:", sys.exc_info( )[0] )

####################################################################################
#   Option validation
####################################################################################

def validate_options( p, vars ):
    op = vars[ 'operation' ]
    incompatible = []

    # options/parameters with defaults, or universally allowed
    always = [ 'access', 'operation', 'ddwrt_file', 'pause_time', 'ota_timeout', 'device_db', 
               'prefix', 'device_queue', 'verbose', 'force_platform' ]

    # options allowed with specific commands
    allow = { "help" : [ "what" ],
              "query" : [ "query_conditions", "query_columns", "group", "set_tag", "match_tag", "delete_tag", "refresh" ],
              "schema" : [ "query_conditions", "query_columns", "group", "match_tag", "refresh" ],
              "apply" : [ "query_conditions", "query_columns", "group", "set_tag", "match_tag", "delete_tag", 
                          "ota", "apply_urls", "refresh", "delete_device", "restore_device", "dry_run", "settings", "access" ],
              "probe-list" : [ "query_conditions", "group", "refresh", "access" ],
              "provision-list" : [ "group", "ddwrt_name", "group", "cue", "timing", "ota", "print_using", "toggle", "wait_time", "settings" ],
              "provision" : [ "ssid", "wait_time", "ota", "print_using", "toggle", "cue", "settings" ],
              "acceptance-test" : [ "ssid" ],
              "config-test" : [ "ssid" ],
              "list" : [ "group" ],
              "clear-list" : [ ]
            }

    # required options for specific commands
    require = { "factory-reset" : [ "device_address" ],
                "identify" : [ "device_address" ],
                "flash" : [ "device_address", "ota" ],
                "list-versions" : [ "device_address" ],
                "ddwrt-learn" : [ "ddwrt_name", "ddwrt_address", "ddwrt_password" ],
                "import" : [ "file" ],
                "replace" : [ "from_device", "to_device" ],
                "print-sample" : [ "print_using" ]
              }

    if op != 'help' and vars[ 'what' ]:
        p.error( "unrecognized arguments: " + " ".join( vars[ 'what' ] ) )

    for r in require:
         if r in allow:
             allow[ r ].extend( require[ r ] )
         else:
             allow[ r ] = require[ r ]

    for z in [ v for v in vars if vars[ v ] and v not in always ]:
        if z not in allow[ op ]:
            incompatible.append( z.replace( "_", "-" ) )
    if len( incompatible ) > 1:
        print( "The options " + ( ','.join( [ "--" + w for w in list( incompatible ) ] ) ) + " are incompatible with the " + op + " operation" )
        sys.exit()
    elif len( incompatible ) == 1:
        print( "The option --" + incompatible[ 0 ] + " is incompatible with the " + op + " operation" )
        sys.exit()

    required = []
    if op in require:
        for r in require[ op ]:
            if r not in vars or not vars[ r ]:
                required.append( r.replace( "_", "-" ) )

    if len( required ) > 1:
        print( "The options " + ( ','.join( [ "--" + w for w in list( required ) ] ) ) + " are required with the " + op + " operation" )
        sys.exit()
    elif len( required ) == 1:
        print( "The option --" + required[ 0 ] + " is required with the " + op + " operation" )
        sys.exit()

####################################################################################
#   Main
####################################################################################


def main():
    global init, wifi_connect, wifi_reconnect, get_cred, router_db, device_queue, device_db
    p = argparse.ArgumentParser( description='Shelly configuration utility' )
    p.add_argument( '-w', '--time-to-wait', dest='wait_time', metavar='0', type=int, default=0, help='Time to wait on each pass looking for new devices, 0 for just once' )
    p.add_argument( '-s', '--ssid', dest='ssid', metavar='SSID', help='SSID of the current WiFi network, where devices are to be connected' )
    p.add_argument( '-p', '--time-to-pause', dest='pause_time', type=int, metavar='2', default=3, help='Time to pause after various provisioning steps' )
    p.add_argument(       '--prefix', dest='prefix', metavar='shelly', default='shelly', help='Prefix for SSID search' )
    p.add_argument( '-v', '--verbose', action='count', default=0, help='Give verbose logging output' )
    p.add_argument( '-V', '--version', action='version', version='version ' + version)
    p.add_argument( '-f', '--file', metavar='FILE', help='File to read/write using IMPORT or EXPORT operation' )
    p.add_argument( '-a', '--device-address',  metavar='TARGET-ADDRESS', help='Address or DNS name of target device' )
    p.add_argument(       '--ddwrt-name', '-N', action='append', metavar='NAME', help='Name of dd-wrt device' )
    p.add_argument( '-g', '--group',  metavar='GROUP', help='Group of devices to apply actions to (as defined in imported file)' )
    p.add_argument( '-e', '--ddwrt-address', metavar='IP-ADDRESS', help='IP address of dd-wrt device to use to configure target device' )
    p.add_argument( '-P', '--ddwrt-password', metavar='PASSWORD', help='Password for dd-wrt device' )
    p.add_argument( '-F', '--force-platform',  metavar='platform', help='Force platform choice: PC|MAC|linux', choices=('PC','MAC','linux') )
    p.add_argument( '-r', '--refresh', action='store_true', help='Refresh the db with attributes probed from device before completing operation' )
    p.add_argument(       '--access', default=None, help='Restrict apply and probe operations to Continuous, Periodic, or ALL devices', choices=['ALL','Continuous','Periodic'] )
    p.add_argument(       '--toggle', action='store_true', help='Toggle relay on devices after each is provisioned' )
    p.add_argument(       '--device-queue', default='provisionlist.json', help='Location of json database of devices to be provisioned with provision-list' )
    p.add_argument(       '--ddwrt-file', default='ddwrt_db.json', help='File to keep ddwrt definitions' )
    p.add_argument(       '--print-using', metavar='PYTHON-FILE', help='Python program file containing a function, "make_label", for labeling provisioned devices' )
    p.add_argument(       '--device-db', default='iot-devices.json', help='Device database file (default: iot-devices.json)' )
    p.add_argument(       '--ota', dest='ota', metavar='http://...|LATEST', default='', help='OTA firmware to update after provisioning, or with "flash" or "apply" operation' )
    p.add_argument( '-n', '--ota-timeout', metavar='SECONDS', default=360, type=int, help='Time in seconds to wait on OTA udpate. Default 360 (6 minutes). Use 0 to skip check (inadvisable)' )
    p.add_argument(       '--url', dest='apply_urls', action='append', help='URL fragments to apply, i.e "settings/?lat=31.366398&lng=-96.721352"' )
    p.add_argument(       '--cue', action='store_true', help='Ask before continuing to provision next device' )
    p.add_argument(       '--timing', action='store_true', help='Show timing of steps during provisioning' )
    p.add_argument( '-q', '--query-columns', help='Comma separated list of columns to output, start with "+" to also include all default columns, "-" to exclude specific defaults' )
    p.add_argument( '-Q', '--query-conditions', help='Comma separated list of name=value selectors' )
    p.add_argument( '-t', '--match-tag', help='Tag to limit query and apply operations' )
    p.add_argument( '-T', '--set-tag', help='Tag results of query operation' )
    p.add_argument(       '--delete-tag', help='Remove tag from results of query operation' )
    p.add_argument(       '--delete-device', metavar='DEVICE-ID|ALL', help='Remove device from device-db' )
    p.add_argument(       '--restore-device', metavar='DEVICE-ID|ALL', help='Restore settings of devices matching query' )
    p.add_argument(       '--from-device', metavar='DEVICE-ID', help='Device db entry from which to copy settings using the replace operation' )
    p.add_argument(       '--to-device', metavar='DEVICE-ID', help='Device db entry to receive the copy of settings using the replace operation' )
    p.add_argument(       '--dry-run', action='store_true', help='Display urls to apply instead of performing --restore or --settings' )
    p.add_argument(       '--settings', help='Comma separated list of name=value settings for use with provision operation' )
    p.add_argument(       metavar='OPERATION', help='|'.join(all_operations), dest="operation", choices=all_operations )

    p.add_argument( dest='what', default=None, nargs='*' )

    try:
        args = p.parse_args( )
    except BaseException as e:
        if e.code != 0:
            print( )
            print( 'Try "python automagic.py features" or "python automagic.py help" for more detailed information.' )
            print( 'Or,... "python automagic.py --help" will give a brief description of all options.' )
        sys.exit()

    validate_options( p, vars( args ) )

    new_version = None

    if not args.access:
        if args.operation in ( 'query' ):
            args.access = 'ALL'
        else:
            args.access = 'Continuous'

    if args.operation == 'help':
        help_docs( args.what )
        return

    if args.operation == 'features':
        help_features( )
        return

    if args.operation in [ 'ddwrt-learn' ] and args.ddwrt_name and len( args.ddwrt_name ) > 1:
        p.error( "only one --ddwrt-name (-N) can be specified for ddwrt-learn operation" )
        return

    if args.operation in [ 'provision-list' ] and args.ddwrt_name and len( args.ddwrt_name ) > 2:
        p.error( "the provision-list operation accepts no more than two --ddwrt-name (-N) options" )
        return

    if args.force_platform:
        platform = args.force_platform
    else:
        if sys.platform == 'darwin':
            platform = 'MAC'
        elif sys.platform == 'win32':
            platform = 'PC'
        elif sys.platform == 'linux':
            platform = 'linux'
        else:
            platform = 'UNKNOWN'

    if platform == 'MAC':
        wifi_connect = mac_wifi_connect
        wifi_reconnect = mac_wifi_reconnect
        init = mac_init
        get_cred = mac_get_cred
    elif platform == 'PC':
        wifi_connect = pc_wifi_connect
        wifi_reconnect = pc_wifi_reconnect
        init = noop
        get_cred = pc_get_cred
    elif platform == 'linux':
        if args.operation == 'provision':
            print( "Provision operation is not yet supported with linux. Use provision-list with --ddwrt-name/-N instead." )
            return
        if args.operation in [ 'provision-list' ] and not args.ddwrt_name:
            print( "With linux, the provision-list operation requires one or two --ddwrt-name (-N) options" )
            return
    else:
        print( "Unsupported OS: " + sys.platform )
        return

    if args.ddwrt_name:
        router_db = read_json_file( args.ddwrt_file, {}, True )
        router_db[ 'Format' ] = 'automagic'

    if args.operation in ( 'import', 'provision-list', 'list', 'probe-list' ):
        device_queue = read_json_file( args.device_queue, [], ['SSID','ProbeIP'] )

    if args.ota and args.ota != 'LATEST':
        if args.verbose > 2: print( "Checking version of OTA firmware" )
        new_version = get_firmware_version( args.ota )
        if new_version:
            print( "OTA firmware build ID: " + new_version )

    if args.print_using:
        import_label_lib( args.print_using )

    if args.operation in [ 'provision-list', 'probe-list', 'query', 'apply', 'schema', 'provision', 'replace' ]:
        device_db = read_json_file( args.device_db, {}, True )
        device_db[ 'Format' ] = 'automagic'

    if args.operation == 'import':
        if not args.file:
            p.error( "import operation requiress -f|--file option" )
            return
        if args.file.lower().endswith('.json'):
            import_json( args.file, args.device_queue )
        else:
            import_csv( args.file, args.device_queue )

    elif args.operation == 'acceptance-test':
        init( )
        credentials = get_cred( )
        try:
            acceptance_test( args, credentials )
        finally:
            print( "Reconnecting to " + credentials['ssid'] )
            wifi_reconnect( credentials )

    elif args.operation in ( 'provision', 'config-test' ) or args.operation == 'provision-list' and not args.ddwrt_name:
        if args.operation == 'config-test' and not args.wait_time: args.wait_time = 15
        init( )
        credentials = get_cred( )
        try:
            provision_native( credentials, args, new_version )
        except SystemExit:
            return
        except:
            print( "Attempting to reconnect to " + credentials['ssid'] + " after failure or control-C" )
            wifi_reconnect( credentials )
            raise

    elif args.operation == 'factory-reset':
        factory_reset( args.device_address, args.verbose )

    elif args.operation == 'flash':
        flash_device( args.device_address, args.pause_time, args.verbose, args.ota, args.ota_timeout, new_version, args.dry_run )

    elif args.operation == 'ddwrt-learn':
        ddwrt_learn( args.ddwrt_name[0], args.ddwrt_address, args.ddwrt_password, args.ddwrt_file )

    elif args.operation == 'provision-list':
        provision_ddwrt( args, new_version )

    elif args.operation == 'list':
        print_list( args.device_queue, args.group )

    elif args.operation == 'clear-list':
        if args.group:
            print( "--group is not yet compatible with clear-list" )
            sys.exit()
        clear_list( args.device_queue )

    elif args.operation == 'print-sample':
         test_print( )

    elif args.operation in ( 'probe-list' ):
        probe_list( args )

    elif args.operation == 'query':
        query( args )

    elif args.operation == 'apply':
        query( args, new_version )

    elif args.operation == 'identify':
        identify( args.device_address )

    elif args.operation == 'list-versions':
        list_versions( args.device_address, args.pause_time, args.verbose )

    elif args.operation == 'schema':
        schema( args )

    elif args.operation == 'replace':
        replace_device( args.device_db, args.from_device, args.to_device )

if __name__ == '__main__':
    try:
        compatibility()
        main() 
    except EOFError as error:
        pass
    except KeyboardInterrupt as error:
        pass

### examples of GUI interaction with DD-WRT device
###   curl --referer http://192.168.1.1/Management.asp -d submit_button=Management -d action=Reboot -u admin:password --http1.1 -v http://192.168.1.1/apply.cgi
###   curl http://192.168.1.1/apply.cgi -d "submit_button=Ping&action=ApplyTake&submit_type=start&change_action=gozila_cgi&next_page=Diagnostics.asp&ping_ip=route+add+-net+21.5.128.0+netmask+255.255.128.0+dev+ppp0" -u admin:admin
###   curl -u admin:pw --referer http://192.168.1.1/Management.asp --http1.1 http://192.168.1.1/apply.cgi -d "submit_button=Status_Internet&action=Apply&change_action=gozila_cgi&submit_type=Disconnect_pppoe
###   curl -u admin:pw --referer http://192.168.1.1/Management.asp --http1.1 http://192.168.1.1/apply.cgi -d "submit_button=index&action=ApplyTake&change_action=&submit_type="

#def ddwrt_apply( tn, mode ):
#    """failed attempt to do everything the "apply" button in the GUI does, to avoid needing to use http/CGI approach (which takes 20s)"""
#    if mode == 'sta':
#        ddwrt_get_single_line_result( tn, "/sbin/ifconfig eth2 192.168.33.10" )
#    ddwrt_get_single_line_result( tn, "stopservice nas;stopservice wlconf;startservice wlconf 2>/dev/null;startservice nas" )
#    if mode == 'sta':
#        ddwrt_get_single_line_result( tn, "route del default netmask 0.0.0.0 dev br0" )
#        ddwrt_get_single_line_result( tn, "route add -net 192.168.33.0 netmask 255.255.255.0 dev eth2" )
#        ddwrt_get_single_line_result( tn, "route add default gw 192.168.33.1 netmask 0.0.0.0 dev br0" )
#        ddwrt_get_single_line_result( tn, "route add default gw 192.168.33.1 netmask 0.0.0.0 dev eth2" )
#    else:
#        ddwrt_get_single_line_result( tn, "route del -net 192.168.33.0 netmask 255.255.255.0 dev eth2" )
#        ddwrt_get_single_line_result( tn, "route del default gw 192.168.33.1 netmask 0.0.0.0 dev br0" )
#        ddwrt_get_single_line_result( tn, "route del default gw 192.168.33.1 netmask 0.0.0.0 dev eth2" )
