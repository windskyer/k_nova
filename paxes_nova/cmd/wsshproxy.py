'''
Created on Mar 16, 2015

@author: root
'''

import sys
import os
import wssh
import paxes_nova

from gevent import monkey
from flask.templating import render_template
monkey.patch_all()

from flask import Flask, request
from werkzeug.exceptions import BadRequest, Unauthorized
#from nova.api.openstack import extensions
from nova.consoleauth import rpcapi as consoleauth_rpcapi
from oslo.config import cfg
from nova import config
from nova import context
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)

root_path = os.path.split(os.path.realpath(paxes_nova.__file__))[0] + '/consoleweb'
template_folder = root_path + '/templates'
static_folder = root_path + '/static'
app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)

opts = [
    cfg.StrOpt('wsshproxy_host',
               default='0.0.0.0',
               help='Host on which to listen for incoming requests'),
    cfg.IntOpt('wsshproxy_port',
               default=6081,
               help='Port on which to listen for incoming requests'),
    cfg.StrOpt('username',
               default='padmin',
               help='the username to authenticate as (defaults to the current local username'),
    cfg.StrOpt('key_filename',
               default='/root/.ssh/id_dsa',
               help='the filename, or list of filenames, of optional private key(s) to \
               try for authentication')]

CONF = cfg.CONF
CONF.register_cli_opts(opts)

@app.route('/')
def ssh():
    endpoint  = request.args.get('endpoint')
    vminfo = request.args.get('vminfo')
    return render_template('index.html', endpoint=endpoint, vminfo=vminfo)

@app.route('/remote')
def index():
    # Abort if this is not a websocket request
    if not request.environ.get('wsgi.websocket'):
        app.logger.error('Abort: Request is not WebSocket upgradable')
        raise BadRequest()

    ctxt = context.get_admin_context()
    _consoleauth_rpcapi = consoleauth_rpcapi.ConsoleAuthAPI()
#     authorize = extensions.extension_authorizer('compute', 'console_auth_tokens')
#     authorize(context)
    # check token
#     # Here you can perform authentication and sanity checks
    token = request.args.get('token')
    connect_info = _consoleauth_rpcapi.check_token(ctxt, token)

    if not connect_info:
        LOG.error("Invalid Token: %s", token)
        raise Exception(_("Invalid Token"))
    host = connect_info["host"]
    lparid = connect_info["port"]
    LOG.debug("connecting to: %s:%s" % (host, lparid))
    # Initiate a WSSH Bridge and connect to a remote SSH server
    username = CONF.username
    key_filename = CONF.key_filename
    bridge = wssh.WSSHBridge(request.environ['wsgi.websocket'])
    try:
        bridge.open(hostname=host,
                    username=username,
                    key_filename=key_filename)
    except Exception as e:
        LOG.error(e)
        request.environ['wsgi.websocket'].close()
        return str()

    # Launch a shell on the remote server and bridge the connection
    # This won't return as long as the session is alive
    init_cmd = "rmvt -id %s" % lparid + ";" + "mkvt -id %s" % lparid + ";" + "exit"
    LOG.debug("run cmd: %s" % init_cmd)
    try:
        bridge.shell(init_cmd)
    except Exception as e:
        LOG.error(e)
        request.environ['wsgi.websocket'].close()
        return str()

    # Alternatively, you can run a command on the remote server
    # bridge.execute('/bin/ls -l /')

    # We have to manually close the websocket and return an empty response,
    # otherwise flask will complain about not returning a response and will
    # throw a 500 at our websocket client
    request.environ['wsgi.websocket'].close()
    return str()


def main():
    from gevent.pywsgi import WSGIServer
    from geventwebsocket.handler import WebSocketHandler
    config.parse_args(sys.argv)
    logging.setup("nova")
    app.debug = True
    host = CONF.wsshproxy_host
    port = CONF.wsshproxy_port
    http_server = WSGIServer((host, port), app,
                             log=None,
                             handler_class=WebSocketHandler)
    print 'Server running on ws://%s:%s/remote' % (host, port)
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        pass
