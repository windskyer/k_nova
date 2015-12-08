from paxes_nova import _
from nova import exception as base_exception

class NotFoundImageID(base_exception.NovaException):
    msg_fmt = _("Failed to image_meta %(image_id)s get image id info")

class CreateImageFailure(base_exception.NovaException):
    msg_fmt = _("Failed to Create image: %(image_id)s")

class NotFoundImageFile(base_exception.NovaException):
    msg_fmt = _("Not Found Image File from %(file)s or is not abs path ")

class NotFoundLocation(base_exception.NovaException):
    msg_fmt = _("Not Found location in image_data ")