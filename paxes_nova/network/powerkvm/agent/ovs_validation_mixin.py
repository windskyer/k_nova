#
# =================================================================
# =================================================================

from nova.openstack.common import log as logging
from oslo.config import cfg
from powervc_nova.network.powerkvm.agent.common import exception

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class OVSValidator():
    """
    Based on the current dom, determine if the passed in values
    are valid.
    """

    def __init__(self, current_dom):
        """
        :param current_dom: A HostOVSNetworkConfig object that represents the
                            current state of the host.
        """
        self.current_dom = current_dom

    def validate_is_not_integration_bridge(self, ovs_name):
        """
        Check if the ovs specified is the integration bridge

        :param ovs_name: Name of the ovs to check.
        """
        if ovs_name == CONF.integration_bridge:
            raise exception.IBMPowerKVMOVSIntBridgeSpecifiedError(
                bridge_name=ovs_name)

    def validate_ovs_exists(self, ovs_name):
        """
        check for existence of specified ovs

        :param ovs_name: Name of the ovs to check.
        """
        for ovs in self.current_dom.ovs_list:
            if ovs.name == ovs_name:
                return

        raise exception.IBMPowerKVMOVSDoesNotExistError(
            switch_name=ovs_name)

    def validate_port_assigned(self, ovs_name, ovs_port_name):
        """
        check if old OVS port is assigned to the ovs

        :param ovs_name: Name of the ovs to check.
        :param ovs_port_name: Name of ovs port to check
        """
        ovs = self.current_dom.contains_vswitch_port_by_name(ovs_port_name)
        if ovs is None or ovs.name != ovs_name:
            #ovs port doesn't exist on switch, generate error
            raise exception.IBMPowerKVMNoOVSPortError(
                port_name=ovs_port_name,
                switch_name=ovs_name)

    def validate_port_unassigned(self, ovs_port_name):
        """
        check if OVS port is already in use somewhere

        :param ovs_port_name: Name of ovs port to check
        """
        ovs = self.current_dom.contains_vswitch_port_by_name(ovs_port_name)
        if ovs:
            #ovs port already assigned to switch, generate error
            raise exception.IBMPowerKVMOVSPortExistsError(
                port_name=ovs_port_name,
                switch_name=ovs.name)

    def validate_adapter_list(self, ovs_port_component_names, ovs_port_name):
        """
        confirm at least one adapter in port list

        :param ovs_port_component_names: A list of the component names
                                         contained within a ovs port.
        :param ovs_port_name: The name of the OVS Port
        """
        if len(ovs_port_component_names) < 1:
            raise exception.IBMPowerKVMOVSNoAdapterSpecified(
                port_name=ovs_port_name)

    def validate_adapters_available(self, ovs_port_component_names):
        """
        check if adapters for port are available for use

        :param ovs_port_component_names: A list of the component names
                                         contained within a ovs port.
        """
        for adpt_nm in ovs_port_component_names:
            if not self.current_dom.contains_unused_port_by_name(adpt_nm):
                # The adapter is already in use, try to figure out what is
                # using it to generate a specific error message.
                parent_name = self.current_dom.find_parent_name(adpt_nm)
                if parent_name:
                    raise exception.IBMPowerKVMOVSParentPortExistsError(
                        adapter_name=adpt_nm,
                        parent_name=parent_name)

                # Couldn't find a specific error message, use a generic one
                raise exception.IBMPowerKVMOVSAdapterNotAvailableError(
                    adapter_name=adpt_nm)

    def validate_adapters_available_update(self,
                                           ovs_port_component_names,
                                           ovs_port_name):
        """
        check if adapters for new port are available for use, either
        by existing in the dom in the unused list or in the
        old port that will be deleted before the new port is
        created

        :param ovs_port_name: ovs port that is being updated
        :param ovs_port_component_names: names of components
            involved in the update
        """

        ovs_dom = self.current_dom.contains_vswitch_port_by_name(ovs_port_name)
        ovs_port = ovs_dom.get_port(self.ovs_port_name)
        current_cmp_list = [ovs_port.port_list[i].name
                            for i in range(0, len(ovs_port.port_list))]

        for adapter_name in ovs_port_component_names:
            if not self.current_dom.contains_unused_port_by_name(adapter_name):
                # check old port port list
                if adapter_name in current_cmp_list:
                    continue

                # The adapter is already in use, try to figure out what is
                # using it to generate a specific error message.
                parent_name = self.current_dom.find_parent_name(adapter_name)
                if parent_name:
                    raise exception.IBMPowerKVMOVSParentPortExistsError(
                        adapter_name=adapter_name,
                        parent_name=parent_name)

                # Couldn't find a specific error message, use a generic one
                raise exception.IBMPowerKVMOVSAdapterNotAvailableError(
                    adapter_name=adapter_name)

    def validate_adapters_appear_once(self,
                                      ovs_port_component_names,
                                      ovs_port_name):
        """
        confirm a component is referenced only once in the component
        list

        :param ovs_port_component_names: A list of the component names
                                         contained within a ovs port.
        """
        totals = {}
        for i in range(0, len(ovs_port_component_names)):
            if ovs_port_component_names[i] not in totals:
                totals[ovs_port_component_names[i]] = 0
                for j in range(i + 1, len(ovs_port_component_names)):
                    if (ovs_port_component_names[i] ==
                            ovs_port_component_names[j]):
                        totals[ovs_port_component_names[i]] += 1

        adapter_list = []
        for adapter in totals.keys():
            if totals[adapter] > 0:
                adapter_list.append(adapter)

        if len(adapter_list) > 0:
            raise exception.IBMPowerKVMOVSAdaptersAppearMultipleTimes(
                adapter_names=adapter_list,
                ovs_port_name=ovs_port_name)

    def validate_ovs_ports_appear_once(self, ovs_name):
        """
        confirm each ovs port is listed only once on
        an ovs

        :param ovs_name: Name of the ovs to check.
        """
        totals = {}
        ovs = self.current_dom.find_port_or_ovs(ovs_name)
        for i in range(0, len(ovs.ovs_port_list)):
            # check to see if we have already processed
            # a port with this name.  if we have, this is
            # a duplicate and we know that the earlier
            # processing already determined this so
            # no need to process again
            if ovs.ovs_port_list[i].name not in totals:
                totals[ovs.ovs_port_list[i].name] = 0
                for j in range(i + 1, len(ovs.ovs_port_list)):
                    if ovs.ovs_port_list[i].name == ovs.ovs_port_list[j].name:
                        totals[ovs.ovs_port_list[i].name] += 1

        port_list = []
        for port_name in totals.keys():
            if totals[port_name] > 0:
                port_list.append(port_name)

        if len(port_list) > 0:
            raise exception.IBMPowerKVMOVSPortsAppearMultipleTimes(
                port_names=port_list,
                ovs_name=ovs_name)

    def validate_ovs_port_appears_on_one_ovs(self, ovs_name):
        """
        confirm each ovs port on this ovs
        is listed only on this ovs

        :param ovs_name: Name of the ovs to check.
        """
        port_tracker_dict = {}
        ovs = self.current_dom.find_port_or_ovs(ovs_name)
        dups_found = False
        for port in ovs.ovs_port_list:
            port_tracker_dict[port.name] = []
            for other_ovs in self.current_dom.ovs_list:
                if other_ovs == ovs:
                    continue
                for other_port in other_ovs.ovs_port_list:
                    if port.name == other_port.name:
                        dups_found = True
                        port_tracker_dict[port.name].append(other_ovs.name)

        if dups_found:
            # construct exception message string
            port_msg = ""
            for port_name, dups in port_tracker_dict.iteritems():
                if len(dups) > 0:
                    port_msg += _("[port name: %s, virtual switches: " %
                                  port_name)
                    for dup in dups:
                        port_msg += "%s, " % dup
                    # get rid of trailing comma
                    port_msg = port_msg[:-2]
                    port_msg += ']'

            raise exception.IBMPowerKVMOVSPortInMultipleOVS(
                port_list=port_msg,
                ovs_name=ovs_name)

    def validate_one_ip_address(self, ovs_name):
        """
        confirm that at most one ovs port has a
        component with an ip address

        :param ovs_name: Name of the ovs to check.
        """
        ovs = self.current_dom.find_port_or_ovs(ovs_name)
        ip_found = False
        for ovs_port in ovs.ovs_port_list:
            if len(ovs_port.ip_addresses) > 0:
                if ip_found:
                    raise exception.IPAddressAddMultiPort(
                        dev=ovs_port.name,
                        ovs_name=ovs_name)
                ip_found = True
            for adapter in ovs_port.port_list:
                if len(adapter.ip_addresses) > 0:
                    if ip_found:
                        raise exception.IPAddressAddMultiPort(
                            dev=adapter.name,
                            ovs_name=ovs_name)
                    ip_found = True
