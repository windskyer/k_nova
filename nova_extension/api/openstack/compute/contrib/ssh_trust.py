
import os

from nova.api.openstack import extensions, extensions, xmlutil, wsgi
from nova.utils import execute
from nova.openstack.common import log as logging
from paxes_nova.compute import HostAPI


''' let the management node can ssh auto-login the compute node '''


LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'ssh_trust')


class HostSshTrustTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('host')
        root.set('host')
        root.set('action')

        return xmlutil.MasterTemplate(root, 1)
    
    
class SshTrustController(object):
    
    def __init__(self):
        self.api = HostAPI()
        super(SshTrustController, self).__init__()
        
    @wsgi.serializers(xml=HostSshTrustTemplate)
    def sshtrust(self, req, id, body=None):
        context = req.environ['nova.context']
        authorize(context)
    
        home = os.path.expanduser("~")
        private_key = home + '/.ssh/id_rsa'
        public_key = home + '/.ssh/id_rsa.pub'
        if not os.path.isfile(private_key):
            args = ['ssh-keygen', '-q', '-t' 'rsa', '-P', '', '-f', private_key]
            execute(*args)
        elif not os.path.isfile(public_key):
            #args = ['ssh-keygen', '-f', private_key, '-y', '>', public_key]
            cmd = 'ssh-keygen -f ' + private_key + ' -y > ' + public_key
            execute(cmd, shell=True)
                                                             
        with open(public_key) as f:
            public_key = f.read()
            
        result = {'host': id}
        try:
            result['action'] = self.api.ssh_trust(context, id, public_key)
        except Exception as e:
            result['action'] = 'sshtrust'
            LOG.exception(e)
        
        return result
       

class Ssh_trust(extensions.ExtensionDescriptor):
    ''' let the management node can ssh auto-login the compute node'''
    
    name = 'ssh-trust'
    alias = 'ssh-trust'
    
    def get_resources(self):
        resources = []
        res = extensions.ResourceExtension('os-hosts',
                        SshTrustController(),
                        member_actions = {'sshtrust': 'GET'}
                        )
        resources.append(res)
        return resources

#     