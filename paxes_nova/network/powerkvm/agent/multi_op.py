#
# =================================================================
# =================================================================

from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm.agent.common import micro_op

LOG = logging.getLogger(__name__)


class MultiMicroOp(micro_op.MicroOperation):
    """
    This class can be used to bundle together several micro operations.  They
    can be run as an independent unit.
    """

    def __init__(self):
        """
        Constructor.
        """
        self.micro_ops = []
        self.has_run = False
        self.has_undone = False

    def validate(self, current_dom):
        """
        Overrides parent method.

        Validate that the operation can be run.
        """
        warnings_list = []
        for op in self.micro_ops:
            current_dom, new_warnings = op.validate(current_dom)
            warnings_list.extend(new_warnings)

        # The dom itself doesn't change here.
        return current_dom, warnings_list

    def execute(self):
        """
        Overrides parent method.

        Execute a command line call to create an OVS port.
        """
        self.has_run = True
        for i in range(0, len(self.micro_ops)):
            try:
                # Try to run each one
                self.micro_ops[i].execute()
            except Exception:
                # Undo the micro ops that were run and raise the exception
                for j in range(i - 1, -1, -1):
                    self.micro_ops[j].undo()
                self.has_undone = True
                raise

    def undo(self):
        """
        Overrides parent method.

        Execute a command line call to remove an OVS port.
        """
        # Undo we can assume all operations have been run.  We just need
        # to run all of our ops in reverse.
        if self.has_run and not self.has_undone:
            for i in range(len(self.micro_ops) - 1, -1, -1):
                self.micro_ops[i].undo()
            self.has_undone = True
