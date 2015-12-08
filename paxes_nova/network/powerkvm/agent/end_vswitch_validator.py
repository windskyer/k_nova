#
# =================================================================
# =================================================================

from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent.common import warning


class EndVswitchValidator(micro_op.MicroOperation):
    """
    Will do the tail end validation of a given Open vSwitch
    """

    def __init__(self, ovs_name):
        """
        Constructor

        :param ovs_name: The name of the Open vSwitch
        """
        self.ovs_name = ovs_name

    def validate(self, current_dom):
        """
        The vSwitch should have a single OVS port.If there are multiple OVS
        Ports on the vSwitch, a WARNING should be thrown.

        :param current_dom: This is the current DOM representation of the
                            system before the micro op is performed.
        :returns updated_dom: An updated version of the current_dom that
                              has been updated 'as if' the operation had been
                              executed.
        :returns warning_list: A list of warnings (but not errors) that were
                               generated based off the state of the DOM and
                               the operation that had been run.
        """
        warning_list = []
        for ovs in current_dom.ovs_list:
            if self.ovs_name == ovs.name:
                if len(ovs.ovs_port_list) > 1:
                    # throw a WARNING if ovs has more than 1 port
                    warning_list.append(warning.OVSMultipleVirtualPortsWarning(
                                        vswitch_name=self.ovs_name))
                break

        return current_dom, warning_list

    def execute(self):
        """
        Overrides parent method.

        This operation is for validation only, execute does nothing.
        """
        pass

    def undo(self):
        """
        Overrides parent method.

        This operation is for validation only, undo does nothing.
        """
        pass
