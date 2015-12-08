'''
Created on Feb 27, 2015

@author: root
'''

from nova.consoleauth import manager

from nova import network


class SSHConsoleAuthManager(manager.ConsoleAuthManager):

    def __init__(self, scheduler_driver=None, *args, **kwargs):
        super(SSHConsoleAuthManager, self).__init__(*args, **kwargs)
        # change for paxes
        self.network_api = network.API()
