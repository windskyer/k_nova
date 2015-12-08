#
# =================================================================
# =================================================================

from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm.agent import end_vswitch_validator
from powervc_nova.network.powerkvm.agent import management_host_ping
from powervc_nova.network.powerkvm.agent import service_network_restart
from powervc_nova.network.powerkvm.agent import ovs_port_add
from powervc_nova.network.powerkvm.agent import ovs_port_remove
from powervc_nova.network.powerkvm.agent import ovs_port_update
from powervc_nova.network.powerkvm.agent import ovs_port_warn_add_remove
from powervc_nova.objects.network import dom_kvm
from powervc_nova.network.powerkvm.agent import move_bridge_ports
from powervc_nova.network.powerkvm.agent import move_ip_address
from powervc_nova.network.powerkvm.agent import beginning_vswitch_validator

from powervc_nova import _

LOG = logging.getLogger(__name__)


class MicroOperationBuilder():
    """
    Takes in a current DOM and a desired DOM, and determines the set of micro
    operations that need to be executed to make the current DOM match the
    desired DOM.
    """

    def __init__(self, context, current_dom, desired_dom, rollback):
        """
        :param context: RPC context object.
        :param current_dom: A HostOVSNetworkConfig object that represents the
                            current state of the host.
        :param desired_dom: A HostOVSNetworkConfig object that represents the
                            desired state of the host.
        :param rollback: Whether the operation should be rolled back before
                         completion, often to test the rollback mechanism.
        """
        self.context = context
        self.current_dom = current_dom
        self.desired_dom = desired_dom
        self.rollback = rollback

    def get_micro_ops_for_update(self):
        """
        Determines the list of micro ops need to transform the current DOM into
        the desired DOM for an HTTP PUT operation, or an "update".  This will
        not create or delete vswitches, but it will update the configuration
        of a vswitch by adding, removing or changing vswitch ports.

        :returns micro_op_list: A list of MicroOperation objects.
        """

        LOG.info(_('Network micro operations builder for network is '
                   'starting.'))

        # Update ports for each vswitch.  Each pass runs against all vSwitches.
        # The first pass is all remove operations.  Followed by update (which
        # may add or remove).  Finally it runs the add pass.

        micro_op_list = []

        # do preliminary validation of each ovs in desired dom
        for ovs in self.desired_dom.ovs_list:
            LOG.info(_('Adding beginning Virtual Switch validate to operation '
                       'list for Virtual Switch %s.' % ovs.name))
            op = beginning_vswitch_validator.\
                BeginningVswitchValidator(ovs.name, self.desired_dom)
            micro_op_list.append(op)

        micro_op_list.extend(self._update_pass(self.current_dom,
                                               self.desired_dom,
                                               self._update_port_remove_pass))
        micro_op_list.extend(self._update_pass(self.current_dom,
                                               self.desired_dom,
                                               self._update_port_update_pass))
        micro_op_list.extend(self._update_pass(self.current_dom,
                                               self.desired_dom,
                                               self._update_port_add_pass))

        # Add any needed micro ops to the end of the op list
        if len(micro_op_list) > 0:
            # If ports were changed, a service network restart is needed
            # as well as end user warnings
            restart = False
            ovs_list = []
            for op in micro_op_list:
                if(isinstance(op, ovs_port_add.OvsPortAdd) or
                   isinstance(op, ovs_port_remove.OvsPortRemove) or
                   isinstance(op, ovs_port_update.OvsPortUpdate)):
                    ovs_list.append(op.ovs_name)
                    restart = True

            if restart:
                LOG.info(_('Adding service network restart to operation '
                           'list.'))
                op = service_network_restart.ServiceNetworkRestart()
                micro_op_list.append(op)
                op = management_host_ping.PingQpid(self.context, self.rollback)
                micro_op_list.append(op)

            # Add a validator for general OVSPort change warnings
            for ovs_name in set(ovs_list):
                op = ovs_port_warn_add_remove.OvsPortWarnAddRemove(ovs_name)
                micro_op_list.append(op)

            # Validate that each vswitch has a valid config
            for ovs in self.desired_dom.ovs_list:
                LOG.info(_('Adding end Virtual Switch validate to operation '
                           'list for Virtual Switch %s.' % ovs.name))
                op = end_vswitch_validator.EndVswitchValidator(ovs.name)
                micro_op_list.append(op)

        return micro_op_list

    def get_micro_ops_for_create(self):
        """
        Determines the list of micro ops need to transform the current DOM into
        the desired DOM for an HTTP POST operation, or a "create".  This will
        create any vswitches that don't currently exist.

        :returns micro_op_list: A list of MicroOperation objects.
        """
        # Not in scope for Paxes 1.2.1
        raise NotImplementedError('Not yet supported')

    def get_micro_ops_for_delete(self):
        """
        Determines the list of micro ops need to transform the current DOM into
        the desired DOM for an HTTP DELETE operation.  This will delete
        vswitches.

        :returns micro_op_list: A list of MicroOperation objects.
        """
        # Not in scope for Paxes 1.2.1
        raise NotImplementedError('Not yet supported')

    def _update_port_remove_pass(self, current_ovs, desired_ovs):
        """
        Determines the micro ops to run for the update port flow, where we are
        removing ports from a given vSwitch.

        :param current_ovs: The current systems Open vSwitch DOM (single
                            vswitch)
        :param desired_ovs: The Open vSwitch DOM for a single vSwitch that the
                            user passed in, which represents what the desired
                            final state will be.
        :return: The micro ops that should be run to delete the desired ports
                 on the vSwitch.
        """

        # Sort the OVS ports into lists based on desired action
        ports_to_delete = self._get_ports_to_delete(current_ovs, desired_ovs)

        micro_op_list = []

        # Remove any ports from the vswitch that are no longer desired.
        for port in ports_to_delete:
            LOG.info(_('Remove port operation for Virtual Switch %(vswitch)s '
                       'and port %(port)s.' %
                       {'vswitch': current_ovs.name,
                        'port': port.name}))
            op = ovs_port_remove.OvsPortRemove(current_ovs.name, port.name)
            micro_op_list.append(op)

        return micro_op_list

    def _update_port_update_pass(self, current_ovs, desired_ovs):
        """
        Determines the micro ops to run for the update port flow, where we are
        modifying (but not adding/removing) ports to a given vSwitch.

        :param current_ovs: The current systems Open vSwitch DOM (single
                            vswitch)
        :param desired_ovs: The Open vSwitch DOM for a single vSwitch that the
                            user passed in, which represents what the desired
                            final state will be.
        :return: The micro ops that should be run to update the desired ports
                 on the vSwitch.
        """
        micro_op_list = []
        prev_voted_ports = []

        # Determine which ports have changes...
        for desired_port in desired_ovs.ovs_port_list:
            current_port = self._find_ovs_port_by_voting(
                desired_port.name,
                desired_port.port_list,
                current_ovs.ovs_port_list,
                prev_voted_ports)
            if current_port:
                prev_voted_ports.append(current_port)
                # The desired port already exists, check for changes
                if desired_port != current_port:
                    pt_names = []
                    for port in desired_port.port_list:
                        # if we are trying to add a linux bridge,
                        # we need to remove ports from bridge to
                        # add directly to vswitch
                        is_bridge = isinstance(port, dom_kvm.LinuxBridge)
                        if is_bridge:
                            op = move_bridge_ports.\
                                MoveBridgePorts(port.name)
                            micro_op_list.append(op)
                            # need to add micro op to move ip address
                            # from bridge to the vswitch
                            op = move_ip_address.MoveIpAddress(
                                port.name, desired_ovs.name)
                            micro_op_list.append(op)
                            for bridge_port in port.port_list:
                                pt_names.append(bridge_port.name)
                        else:
                            pt_names.append(port.name)
                    op = ovs_port_update.OvsPortUpdate(current_ovs.name,
                                                       current_port.name,
                                                       desired_port.name,
                                                       pt_names)
                    micro_op_list.append(op)
                    LOG.info(_('Update port operation for Virtual Switch '
                               '%(vswitch)s and port %(port)s.' %
                               {'vswitch': current_ovs.name,
                                'port': current_port.name}))

        return micro_op_list

    def _update_port_add_pass(self, current_ovs, desired_ovs):
        """
        Determines the micro ops to run for the update port flow, where we are
        adding ports to a given vSwitch.

        :param current_ovs: The current systems Open vSwitch DOM (single
                            vswitch)
        :param desired_ovs: The Open vSwitch DOM for a single vSwitch that the
                            user passed in, which represents what the desired
                            final state will be.
        :return: The micro ops that should be run to add the desired ports
                 to the vSwitch.
        """
        micro_op_list = []
        prev_voted_ports = []

        # Find ports to create
        ports_to_create = {}
        for desired_port in desired_ovs.ovs_port_list:
            ovs_port = self._find_ovs_port_by_voting(desired_port.name,
                                                     desired_port.port_list,
                                                     current_ovs.ovs_port_list,
                                                     prev_voted_ports)
            if not ovs_port and desired_port and desired_port.name:
                cmp_list = []
                for port in desired_port.port_list:
                    # if we are trying to add a linux bridge,
                    # we need to remove ports from bridge to
                    # add directly to vswitch
                    is_bridge = isinstance(port, dom_kvm.LinuxBridge)
                    if is_bridge:
                        op = move_bridge_ports.MoveBridgePorts(port.name)
                        micro_op_list.append(op)
                        # need to add micro op to move ip address
                        # from bridge to the vswitch
                        op = move_ip_address.MoveIpAddress(port.name,
                                                           desired_ovs.name)
                        micro_op_list.append(op)

                        # Since the request came in to add all the bridge ports
                        # to this port, rather than break them out into unique
                        # OVS ports (like one may expect) we add them into
                        # the bonded port.
                        #
                        # This is safer so as not to create loops.
                        for bridge_port in port.port_list:
                            cmp_list.append(bridge_port.name)
                    else:
                        cmp_list.append(port.name)

                if len(cmp_list) > 0:
                    ports_to_create[desired_port.name] = cmp_list
            elif ovs_port:
                prev_voted_ports.append(ovs_port)

        # Add new ports to the vswitch.
        for port in ports_to_create:
            LOG.info(_('Add port operation for Virtual Switch %(vswitch)s '
                       'and port %(port)s.' %
                       {'vswitch': current_ovs.name, 'port': port}))
            cmp_names = []
            for comp in ports_to_create[port]:
                cmp_names.append(comp)
            op = ovs_port_add.OvsPortAdd(current_ovs.name, port, cmp_names)
            micro_op_list.append(op)

        return micro_op_list

    def _update_pass(self, current_dom, desired_dom, func):
        """
        This method is used by the 'update' path.  It will be used to find
        from the current host DOM the corresponding desired Host DOM, and then
        will run the function passed in against it.

        :param current_dom: The current system's Host DOM.
        :param desired_dom: The Host DOM that the user passed in, which
                            represents what the desired final state will be.
        :param func: The function to run against it.  Should take in a
                     current_ovs and the desired_ovs and return a list of
                     micro ops to run.
        :return micro_op_list: The list of micro ops generated by the function.
        """
        micro_op_list = []
        for desired_ovs in desired_dom.ovs_list:
            for current_ovs in current_dom.ovs_list:
                if desired_ovs.name == current_ovs.name:
                    ops = func(current_ovs, desired_ovs)
                    micro_op_list.extend(ops)
        return micro_op_list

    def _get_ports_to_delete(self, current_ovs, desired_ovs):
        """
        Inspect the current and desired OVS DOM object and determine which
        ports would need to be deleted from the current OVS to achieve the
        desired OVS.

        :param current_ovs: An OpenVSwitch dom_kvm object representing the
                            current state of the vswitch.
        :param desired_ovs: An OpenVSwitch dom_kvm object representing the
                            desired state of the vswitch.
        :returns: A list of OVSPort objects to delete.
        """
        # Any port that is not being updated is being deleted.
        # Find a list of ports that will be updated.
        prev_voted_ports = []
        for desired_port in desired_ovs.ovs_port_list:
            current_port = self._find_ovs_port_by_voting(
                desired_port.name,
                desired_port.port_list,
                current_ovs.ovs_port_list,
                prev_voted_ports)
            if current_port:
                prev_voted_ports.append(current_port)

        # Find any ports that aren't being updated and remove them.
        ports_to_delete = []
        for current_port in current_ovs.ovs_port_list:
            if not current_port in prev_voted_ports:
                ports_to_delete.append(current_port)
        return ports_to_delete

    def _find_ovs_port_by_voting(self, desired_port_name, desired_cmp_list,
                                 current_port_obj_list, prev_voted_ports):
        """
        Use a heuristic approach to finding an OVSPort object.  A matching name
        counts as one vote and a matching child counts as one vote.  The
        OVSPort object with the higest number of votes wins.  Ties are broken
        by picking the first tied OVSPort in the list.

        :param desired_port_name: The name of the port passed in on the REST
                                  API by the caller.  Matching on this is worth
                                  1 vote.
        :param desired_cmp_list: The list of child ports passed in on the REST
                                 API by the caller.  Each match here is worth 1
                                 vote.
        :param current_port_obj_list: The existing list of OVSPorts on the
                                      vswitch.  This will be searched to
                                      find the best match with the other
                                      params.
        :param prev_voted_ports: Each OVS port should only be used once, to
                                 avoid multiple updates to the same port.  This
                                 is a list of ports that should not be selected
                                 due to having already been used.
        :return best_match: An OVSPort from current_port_obj_list that best
                            matches desired_port_name and desired_cmp_list, or
                            None if no suitable match was found.
        """
        # Keep track of the best match that we find
        highest_vote = 0
        best_match = None

        # Check each port for the best match
        for current_port in current_port_obj_list:

            # Reset voting
            vote = 0

            # Matching port name is 1 vote
            if desired_port_name == current_port.name:
                vote = vote + 1

            # Each matching child component is 1 vote
            for desired_cmp in desired_cmp_list:
                for child_port in current_port.port_list:
                    if desired_cmp.name == child_port.name:
                        vote = vote + 1

            # If the desired_port_name is the name of an adapter, and the
            # adapter is not in the desired_cmp_list...then its not a valid
            # update.  It should be treated as a remove and add.
            desired_cmp_names = []
            for comp in desired_cmp_list:
                desired_cmp_names.append(comp.name)
            if desired_port_name not in desired_cmp_names and\
                    self.current_dom.contains_port_by_name(desired_port_name):
                continue

            # Skip this port if it has already been used
            if current_port in prev_voted_ports:
                continue

            # Check for a new high score
            if vote > highest_vote:
                highest_vote = vote
                best_match = current_port

        return best_match
