# vim: tabstop=4 shiftwidth=4 softtabstop=4
# Copyright 2011 Nicira Networks, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# @author: Somik Behera, Nicira Networks, Inc.
# @author: Brad Hall, Nicira Networks, Inc.
# @author: Dan Wendlandt, Nicira Networks, Inc.

import ConfigParser
import logging as LOG
import os
import sys

from quantum.quantum_plugin_base import QuantumPluginBase
from optparse import OptionParser

import quantum.db.api as db
import ovs_db

CONF_FILE = "ovs_quantum_plugin.ini"

LOG.basicConfig(level=LOG.WARN)
LOG.getLogger("ovs_quantum_plugin")


def find_config(basepath):
    for root, dirs, files in os.walk(basepath):
        if CONF_FILE in files:
            return os.path.join(root, CONF_FILE)
    return None


class VlanMap(object):
    vlans = {}

    def __init__(self):
        for x in xrange(2, 4094):
            self.vlans[x] = None

    def set(self, vlan_id, network_id):
        self.vlans[vlan_id] = network_id

    def acquire(self, network_id):
        for x in xrange(2, 4094):
            if self.vlans[x] == None:
                self.vlans[x] = network_id
                # LOG.debug("VlanMap::acquire %s -> %s" % (x, network_id))
                return x
        raise Exception("No free vlans..")

    def get(self, vlan_id):
        return self.vlans[vlan_id]

    def release(self, network_id):
        for x in self.vlans.keys():
            if self.vlans[x] == network_id:
                self.vlans[x] = None
                # LOG.debug("VlanMap::release %s" % (x))
                return
        LOG.error("No vlan found with network \"%s\"" % network_id)


class OVSQuantumPlugin(QuantumPluginBase):

    def __init__(self, configfile=None):
        config = ConfigParser.ConfigParser()
        if configfile == None:
            if os.path.exists(CONF_FILE):
                configfile = CONF_FILE
            else:
                configfile = find_config(os.path.abspath(
                        os.path.dirname(__file__)))
        if configfile == None:
            raise Exception("Configuration file \"%s\" doesn't exist" %
              (configfile))
        LOG.debug("Using configuration file: %s" % configfile)
        config.read(configfile)
        LOG.debug("Config: %s" % config)

        DB_NAME = config.get("DATABASE", "name")
        DB_USER = config.get("DATABASE", "user")
        DB_PASS = config.get("DATABASE", "pass")
        DB_HOST = config.get("DATABASE", "host")
        options = {"sql_connection": "mysql://%s:%s@%s/%s" % (DB_USER,
          DB_PASS, DB_HOST, DB_NAME)}
        db.configure_db(options)

        self.vmap = VlanMap()
        # Populate the map with anything that is already present in the
        # database
        vlans = ovs_db.get_vlans()
        for x in vlans:
            vlan_id, network_id = x
            # LOG.debug("Adding already populated vlan %s -> %s"
            #                                   % (vlan_id, network_id))
            self.vmap.set(vlan_id, network_id)

    def get_all_networks(self, tenant_id):
        nets = []
        for x in db.network_list(tenant_id):
            LOG.debug("Adding network: %s" % x.uuid)
            d = {}
            d["net-id"] = str(x.uuid)
            d["net-name"] = x.name
            nets.append(d)
        return nets

    def create_network(self, tenant_id, net_name):
        d = {}
        try:
            res = db.network_create(tenant_id, net_name)
            LOG.debug("Created newtork: %s" % res)
        except Exception, e:
            LOG.error("Error: %s" % str(e))
            return d
        d["net-id"] = str(res.uuid)
        d["net-name"] = res.name
        vlan_id = self.vmap.acquire(str(res.uuid))
        ovs_db.add_vlan_binding(vlan_id, str(res.uuid))
        return d

    def delete_network(self, tenant_id, net_id):
        net = db.network_destroy(net_id)
        d = {}
        d["net-id"] = str(net.uuid)
        ovs_db.remove_vlan_binding(net_id)
        self.vmap.release(net_id)
        return d

    def get_network_details(self, tenant_id, net_id):
        ports = db.port_list(net_id)
        ifaces = []
        for p in ports:
            ifaces.append(p.interface_id)
        return ifaces

    def rename_network(self, tenant_id, net_id, new_name):
        try:
            net = db.network_rename(net_id, tenant_id, new_name)
        except Exception, e:
            raise Exception("Failed to rename network: %s" % str(e))
        d = {}
        d["net-id"] = str(net.uuid)
        d["net-name"] = net.name
        return d

    def get_all_ports(self, tenant_id, net_id):
        ids = []
        ports = db.port_list(net_id)
        for x in ports:
            LOG.debug("Appending port: %s" % x.uuid)
            d = {}
            d["port-id"] = str(x.uuid)
            ids.append(d)
        return ids

    def create_port(self, tenant_id, net_id, port_state=None):
        LOG.debug("Creating port with network_id: %s" % net_id)
        port = db.port_create(net_id)
        d = {}
        d["port-id"] = str(port.uuid)
        LOG.debug("-> %s" % (port.uuid))
        return d

    def delete_port(self, tenant_id, net_id, port_id):
        try:
            port = db.port_destroy(port_id)
        except Exception, e:
            raise Exception("Failed to delete port: %s" % str(e))
        d = {}
        d["port-id"] = str(port.uuid)
        return d

    def update_port(self, tenant_id, net_id, port_id, port_state):
        """
        Updates the state of a port on the specified Virtual Network.
        """
        LOG.debug("update_port() called\n")
        port = db.port_get(port_id)
        port['port-state'] = port_state
        return port

    def get_port_details(self, tenant_id, net_id, port_id):
        port = db.port_get(port_id)
        rv = {"port-id": port.uuid, "attachment": port.interface_id,
          "net-id": port.network_id, "port-state": "UP"}
        return rv

    def plug_interface(self, tenant_id, net_id, port_id, remote_iface_id):
        db.port_set_attachment(port_id, remote_iface_id)

    def unplug_interface(self, tenant_id, net_id, port_id):
        db.port_set_attachment(port_id, "")

    def get_interface_details(self, tenant_id, net_id, port_id):
        res = db.port_get(port_id)
        return res.interface_id
