#
#
# =================================================================
# =================================================================

"""Extended Compute Driver interface to discover/query resource information"""


class ComputeDiscoveryDriver(object):
    """
    Extended Compute Driver interface for drivers to implementation to
    discover/query additional resource information from the managed system.

    The base ComputeDriver interface provides methods to take actions on the
    Host and its VM's but given the premise that OpenStack is the authoritative
    management source for the Hosts being managed, it assumes the resources
    created by OpenStack are an accurate representation of the current state of
    the resources, so it provides very limited information through the driver
    interface about those resources.

    This interface extends those driver capabilities by asking the driver to
    provide 4 levels of information about the resources being managed:
        1) discover - provide a list of all VM's that exist on the Host
        2) query - provide enough info about the VM's to import into OpenStack
        3) inventory - provide additional details about the VM's specified
        4) metrics - provide additional metric information about the VM's
    """

    def discover_instances(self, context):
        """
        Returns a list of all of the VM's that exist on the given Host.
        For each VM the driver needs to return a dictionary containing
        the following attributes:
            name:      The Name of the VM defined on the Hypervisor
            state:     The State of the VM, matching the power_state definition
            uuid:      The UUID of the VM when created thru OS (Optional)
            hostuuid:  The Hypervisor-specific UUID of the VM
            support:   Dictionary stating whether the VM can be managed
              status:  Whether or not it is "supported" or "not_supported"
              reasons: List of Text Strings as to why it isn't supported

        :param context:   The security context for the query
        """
        raise NotImplementedError()

    def query_instances(self, context, instances):
        """
        Returns a list of VM's (matching those specified on input) with
        enough additional details about each VM to be able to import the
        VM's into the Nova Database such as OpenStack can start managing.
        For each VM the driver needs to return a dictionary containing
        the following attributes:
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            state:     The State of the VM, matching the power_state definition
            hostuuid:  The Hypervisor-specific UUID of the VM
            vcpus:     The number of Virtual CPU's currently allocated
            memory_mb: The amount of Memory currently allocated
            root_gb:   The amount of storage currently allocated
            ephemeral_gb:  The amount of ephemeral storage currently allocated
            flavor:    Dictionary containing the profile/desired allocations
              vcpus:     The number of Virtual CPU's defined for allocation
              memory_mb: The amount of Memory defined for allocation
              root_gb:   The amount of storage defined for allocation
              ephemeral_gb:  The ephemeral storage defined for allocation
              powervm:proc_units:  The number of proc_units defined (Optional)
            volumes:   List of dictionary objects with the attached volumes:
              uuid:      The UUID of the Volume when created thru OS (Optional)
              name:      The Name of the Volume defined on the Backend
              host:      The registered name of the Storage Provider
              provider_location:  The Volume ID known on the Backend (Optional)
            ports:     List of dictionary objects with the network ports:
              mac_address:  The MAC Address of the Port allocated to the VM
              status:       The Status of the Port, matching the definition
              segmentation_id: The VLAN ID for the Network the Port is part of
              physical_network: The Physical Network the Port is part of

        :param context:   The security context for the query
        :param instances: A list of dictionary objects for each VM containing:
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            hostuuid:  The Hypervisor-specific UUID of the VM
        """
        raise NotImplementedError()

    def inventory_instances(self, context, instances):
        """
        Provides a mechanism for the Driver to gather Inventory-related
        information for the Instances provided off of the Hypervisor at
        periodic intervals.  The Driver is free from there to populate
        the information directly in the Database rather than return it.

        :param context: The security context for the query
        :param instances: A list of dictionary objects for each VM containing:
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            hostuuid:  The Hypervisor-specific UUID of the VM
        """
        pass

    def monitor_instances(self, context, instances):
        """
        Returns details about the VM's specified that can be considered
        an accurate inventory of the VM that is periodically collected.
        For each VM the driver needs to return a dictionary containing
        the following attributes:
            uuid:      The UUID of the Instance that was passed in
            name:      The Name of the Instance that was passed in
            cpu_utilization: The percent of CPU that is being used

        :param context: The security context for the query
        :param instances: A list of dictionary objects for each VM containing:
            uuid:      The UUID of the VM when created thru OpenStack
            name:      The Name of the VM when created thru OpenStack
        """
        return []

    def handle_evacuated_instances(self, context, db_instances, drv_instances):
        """
        Allows the Driver to do any cleanup or updating of instances when
        they were changed or evacuated outside of the Management Stack.

        :param context: The security context for the operations
        :param db_instances: A list of Instances known in the OpenStack DB
        :param drv_instances: A list of Instances existing on the Hypervisor
        """
        pass

    def inventory_host(self, context):
        """
        Provides a mechanism for the Driver to gather Inventory-related
        information for the Host at periodic intervals.  The Driver is
        free from there to populate the information directly in the
        Database and should not return any information from the method.

        :param context: The security context for the query
        """
        pass

    def is_inventory_needed(self, context):
        '''
        Some drivers may implement this method to more quickly determine if
        a full inventory is needed.  For those that don't implement this
        method, it'll default to returning False.
        '''
        return False

    def unmanage_host(self, context):
        """Allows the Driver to do any cleanup when a Host is being removed"""
        pass

    def cleanup_network_associations(self, context, host):
        """
        cleans up network associations of deleted networks
        """
        pass

    def post_registration(self, context):
        """
         This method is run after the system is first registered.  It should
         only be run once.  Subsequent restarts of the nova agent will not
         invoke this.

         :param context: The nova context.
         """
        pass
