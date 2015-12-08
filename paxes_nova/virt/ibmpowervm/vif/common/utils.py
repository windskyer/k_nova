# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================
from paxes_nova.db import api as dom_api
from paxes_nova.db.network import models as dom_model

# maximum number of VEA allowed per SEA.  We will artificially restrict this
# to 15 (rather than the actual limit of 16) in order to avoid the case where
# we may create a VEA only to find the SEA has no more room for it and thus
# leaving an orphan.  The reality is that customers will not hit this limit
# of (15 VEAs) * (19 vlans per VEA) anyways.
SEA_MAX_VEAS = 15
# maximum number of 8021Q tagged VLAN id supported on a single VEA
VEA_MAX_VLANS = 19


def is_valid_vlan_id(vlan_id):
    """
    check whether vlan_id is in the range of [1,4094]

    :param vlan_id: vlan id to validate
    :returns: True or False
    """

    return True if vlan_id and (vlan_id > 0 and vlan_id < 4095) else False


def get_host_dom(context, host_id, session=None):
    """
    Will query the system for a given host and will return its corresponding
    DOM object.

    :param context: The context for the query
    :param host_id: The host identifier
    :param session: Optional session to keep the objects within a single
                    transaction
    """
    vio_servers = dom_api.vio_server_find_all(context, host_id, session)
    return dom_model.Host(host_id, vio_servers)


def find_mapping_net_assn(context, network_id, host_id):
    """
    Takes in a network id and the host id to return an SEA that creates that
    mapping. If there's no association found, a None is returned

    :context: The context used to call the dom API
    :network_id: The neutron network id.
    :host_id: The Host id of the host, for which we want the
    association.
    :returns: The associated Network Association.  May be None
    """
    return dom_api.network_association_find(context, host_id, network_id)


def is_vlan_in_primary_vea_addl_vlans(vlanid, primary_sea):
    """
    This API is used to find out if the supplied VLANID exists in the SEA's
    primary VEA's additional VLANIDs. A True is returned if the VLAN is found.

    :param vlanid: The vlan to search
    :param primary_sea: The SEA to search the VLANs.
    :returns: True or False.
    """

    if primary_sea:
        addl_vlans = primary_sea.get_primary_vea().addl_vlan_ids
        if int(vlanid) in addl_vlans:
            return True
    return False


def vios_minimum_requirements():
    """
    This method takes in HMC details and outputs the minimum recommended
    attributes a VIOS should meet to be managed by this HMC.
    """
    pass
