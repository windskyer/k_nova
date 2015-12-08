#
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


"""IBMPowerVM manager commands."""


class BaseCommand(object):

    def lsvg(self, args=''):
        return 'lsvg %s' % args

    def mklv(self, args=''):
        return 'mklv %s' % args

    def rmdev(self, args=''):
        return 'rmdev %s' % args

    def rmvdev(self, args=''):
        return 'rmvdev %s' % args

    def lsmap(self, args=''):
        return 'lsmap %s' % args

    def lsdev(self, args=''):
        return 'lsdev %s' % args

    def rmsyscfg(self, args=''):
        return 'rmsyscfg %s' % args

    def chsysstate(self, args=''):
        return 'chsysstate %s' % args

    def mksyscfg(self, args=''):
        return 'mksyscfg %s' % args

    def lssyscfg(self, args=''):
        return 'lssyscfg %s' % args

    def cfgdev(self, args=''):
        return 'cfgdev %s' % args

    def chdev(self, args=''):
        return 'chdev %s' % args

    def mkvdev(self, args=''):
        return 'mkvdev %s' % args

    def lshwres(self, args=''):
        return 'lshwres %s' % args

    def hostname(self, args=''):
        return 'hostname %s' % args

    def vhost_by_instance_id(self, instance_id_hex):
        pass

    def chsyscfg(self, args=''):
        return 'chsyscfg %s' % args

    def lslparutil(self, args=''):
        return 'lslparutil %s' % args

    def lsrep(self, args=''):
        return 'lsrep %s' % args

    def mkrep(self, args=''):
        return 'mkrep %s' % args

    def mkvopt(self, args=''):
        return 'mkvopt %s' % args

    def rmvopt(self, args=''):
        return 'rmvopt %s' % args

    def loadopt(self, args=''):
        return 'loadopt %s' % args

    def unloadopt(self, args=''):
        return 'unloadopt %s' % args

    def migrlpar(self, args=''):
        return 'migrlpar %s' % args

    def lslparmigr(self, args=''):
        return 'lslparmigr %s' % args

    def lsrefcode(self, args=''):
        return 'lsrefcode %s' % args

    def sysstat(self, args=''):
        """
        Returns a string of the formatted sysstat command to run.
        Typically this command should be run with the -short option
        and a User operand should be provided to narrow the results.
        :returns: string - formatted sysstat command
        """
        return 'sysstat %s' % args


class IVMCommand(BaseCommand):

    def lsvg(self, args=''):
        return 'ioscli ' + BaseCommand.lsvg(self, args)

    def mklv(self, args=''):
        return 'ioscli ' + BaseCommand.mklv(self, args)

    def rmdev(self, args=''):
        return 'ioscli ' + BaseCommand.rmdev(self, args)

    def rmvdev(self, args=''):
        return 'ioscli ' + BaseCommand.rmvdev(self, args=args)

    def lsmap(self, args=''):
        return 'ioscli ' + BaseCommand.lsmap(self, args)

    def lsdev(self, args=''):
        return 'ioscli ' + BaseCommand.lsdev(self, args)

    def cfgdev(self, args=''):
        return 'ioscli ' + BaseCommand.cfgdev(self, args=args)

    def chdev(self, args=''):
        return 'ioscli ' + BaseCommand.chdev(self, args=args)

    def vhost_by_instance_id(self, instance_id_hex):
        command = self.lsmap(args='-all | grep \'%s\' | awk \'{print $1}\''
                             % instance_id_hex)
        return command

    def mkvdev(self, args=''):
        return 'ioscli ' + BaseCommand.mkvdev(self, args=args)

    def hostname(self, args=''):
        return 'ioscli ' + BaseCommand.hostname(self, args=args)

    def lslparutil(self, args=''):
        return BaseCommand.lslparutil(self, args)

    def lsrep(self, args=''):
        return 'ioscli ' + BaseCommand.lsrep(self, args)

    def mkrep(self, args=''):
        return 'ioscli ' + BaseCommand.mkrep(self, args)

    def mkvopt(self, args=''):
        return 'ioscli ' + BaseCommand.mkvopt(self, args)

    def rmvopt(self, args=''):
        return 'ioscli ' + BaseCommand.rmvopt(self, args)

    def loadopt(self, args=''):
        return 'ioscli ' + BaseCommand.loadopt(self, args)

    def unloadopt(self, args=''):
        return 'ioscli ' + BaseCommand.unloadopt(self, args)

    def lsrefcode(self, args=''):
        return BaseCommand.lsrefcode(self, args)

    def sysstat(self, args=''):
        return 'ioscli ' + BaseCommand.sysstat(self, args=args)

    def migrlpar(self, args=''):
        return BaseCommand.migrlpar(self, args)

    def lslparmigr(self, args=''):
        return BaseCommand.lslparmigr(self, args)
