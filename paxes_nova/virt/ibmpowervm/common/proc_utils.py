# =================================================================
# =================================================================

"""Processor related utilities"""

# Multiplier for processor units for virtual cpus
IBM_POWERVM_VCPU_MULTIPLIER = 0.5


def check_proc_compat_mode_active_migration(preferred_mode=None,
                                            current_mode=None,
                                            supported_modes=[]):
    """Checks whether an LPAR can live migrate to the dest host based upon its
       supported compatibility modes

       :param preferred_mode: preferred compat mode of the LPAR
       :param current_mode: current compat mode of the LPAR
       :param supported_modes: proc compat modes supported by the dest host
       :returns: True if the LPAR can migrate and False otherwise
    """

    def _check_current_compat_mode(current_mode, supported_modes):
        return current_mode in supported_modes

    return (_check_current_compat_mode(current_mode, supported_modes) and
            _check_preferred_compat_mode(preferred_mode, supported_modes))


def check_proc_compat_mode_inactive_migration(preferred_mode=None,
                                              supported_modes=[]):
    """Checks whether an LPAR can cold migrate to the dest host based upon its
       supported compatibility modes

       :param preferred_mode: preferred compat mode of the LPAR
       :param supported_modes: proc compat modes supported by the dest host
       :returns: True if the LPAR can migrate and False otherwise
    """

    return _check_preferred_compat_mode(preferred_mode, supported_modes)


def _check_preferred_compat_mode(preferred_mode, supported_modes):
    """Checks whether the LPAR's preferred mode is supported

       :param preferred_mode: preferred compat mode of the LPAR
       :param supported_modes: proc compat modes supported by the dest host
       :returns: True if the preferred mode is supported and False otherwise
    """

    if preferred_mode == 'default':
        return True
    return preferred_mode in supported_modes


def get_num_proc_units(vcpus):
    """Get the default # of proc units given the # of vcpus.

       :param vcpus: Number of virtual cpus (int)
       :returns: number of processor units (float)
    """

    return IBM_POWERVM_VCPU_MULTIPLIER * vcpus
