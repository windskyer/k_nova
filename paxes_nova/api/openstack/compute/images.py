import os
from six.moves.urllib import parse as urlparse
from nova.api.openstack import wsgi
from nova.api.openstack.compute import images as base_images
from nova.openstack.common.uuidutils import generate_uuid

from paxes_nova import compute
from paxes_nova import exception

class Controller(base_images.Controller):
    def __init__(self, image_service=None, **kwargs):
        """Initialize new `ImageController`.

        :param image_service: `nova.image.glance:GlanceImageService`

        """
        super(Controller, self).__init__(image_service=image_service, **kwargs)
        self.compute_api = compute.API()

    @wsgi.serializers(xml=base_images.ImageTemplate)
    def create(self, req, body):
        context = req.environ.get('nova.context')
        is_back = body.get('is_back', False)
        image_meta = body.get('image_meta', {})
        image_data = body.get('image_data', {})

        if not image_meta.get('is_public', None):
            image_meta['is_public'] = True

        if not image_meta.get('name', None):
            image_meta['name'] = 'Nova_Image_' + generate_uuid()[:8]

        if not image_meta.get('container_format', None):
            image_meta['container_format'] = 'bare'

        if not image_meta.get('disk_format', None):
            image_meta['disk_format'] = 'raw'

        if not image_meta.get('properties', None):
            image_meta['properties'] = {'os_distro' : 'rhel' }

        location = image_data.get('location', None)
        if not location:
            raise exception.NotFoundLocation()

        pieces = urlparse.urlparse(location)
        assert pieces.scheme in ('http', 'https' ,'file')
        is_open = False
        if pieces.scheme in ('file'):
            destfile = pieces.path
            if not (os.path.isabs(destfile) and os.path.exists(destfile)):
                raise exception.NotFoundImageFile(file=destfile)
            else:
                is_open = True
        else:
            is_open = False
        image = self._image_service.create(context,
                                           image_meta,
                                           data=None)
        if is_open:
            image_id = image.get('id')
            with open(destfile, 'r') as img_file:
                image = self._image_service.update(context,
                                                   image_id,
                                                   image_meta,
                                                   img_file)
        if not is_back:
            return image
        image_data['is_back'] = is_back
        image_data['image_id'] = image.get('id')
        image_data['size'] = image.get('size')
        self.compute_api.image_create(context, image_meta, image_data)
        return image

def create_resource():
    return wsgi.Resource(Controller())
