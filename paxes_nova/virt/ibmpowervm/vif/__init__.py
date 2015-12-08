# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================


def parse_to_int(value):
    """
    Used to remove invalid data from the string

    :param value: A String value, perhaps with quotes
    :return: An integer representing the alue
    """
    return int(value.strip().strip("\""))
