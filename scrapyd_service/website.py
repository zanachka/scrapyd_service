from datetime import datetime, timedelta
import socket
from twisted.web import resource, static
from twisted.application.service import IServiceCollection
from scrapy.utils.misc import load_object
from .interfaces import IPoller, IEggStorage, ISpiderScheduler, IHostPinger
from .utils import get_resources
from six.moves.urllib.parse import urlparse
import json
import dateparser


class Root(resource.Resource):

    def __init__(self, config, app):
        resource.Resource.__init__(self)
        self.debug = config.getboolean('debug', False)
        self.runner = config.get('runner')
        self.setting = dict(config.items("settings"))
        self.cfg_resources = dict(config.items("cfg"))
        logsdir = config.get('logs_dir')
        itemsdir = config.get('items_dir')
        local_items = itemsdir and (
            urlparse(itemsdir).scheme.lower() in ['', 'file'])
        self.app = app
        # localhost
        bind_address = config.get("bind_address", "127.0.0.1")
        port = config.get("http_port", 6800)
        self.master_host = f"{bind_address}:{port}"
        # cluster
        cluster = dict(config.items("cluster", ()))
        self.cluster_name = cluster.get("cluster_name")
        self.node_name = cluster.get('node_name', socket.gethostname())
        self.identity = cluster.get("identity", "slave")
        # git
        self.all_code_is_same = cluster.get("is_all_code_same") == "True" or \
            cluster.get("is_all_code_same") == "False" or \
            json.loads(cluster.get("is_all_code_same", "true"))
        self.pull_code_by_git = cluster.get("pull_code_by_git") == "True" or \
            cluster.get("is_all_code_same") == "False" or \
            json.loads(cluster.get("pull_code_by_git", "true"))
        self.git_branch = cluster.get("branch", "master")
        self.local_crawler_code_path = cluster.get("local_crawler_code_path")
        # auth
        username = config.get("username")
        password = config.get("password")
        if username and password:
            self.auth = {
                "username": username,
                "password": password
            }
        else:
            self.auth = None
        # proxy scrapyd_service.proxy
        if self.identity == "master":
            self.proxy_runner = config.get("proxy_runner")
            self.proxy_expire = config.get("proxy_expire")
        # home
        self.putChild(b'', Home(self, local_items))
        if logsdir:
            self.putChild(b'logs', static.File(
                logsdir.encode('ascii', 'ignore'), 'text/plain'))
        if local_items:
            self.putChild(b'items', static.File(itemsdir, 'text/plain'))
        # jobs
        self.putChild(b'jobs', Jobs(self, local_items))
        # services
        services = config.items(f'{self.identity}-services', ()) or \
            config.items('services', ())
        for servName, servClsName in services:
            servCls = load_object(servClsName)
            self.putChild(servName.encode('utf-8'), servCls(self))
        self.update_projects()

    def update_projects(self):
        self.poller.update_projects()
        self.scheduler.update_projects()

    @property
    def launcher(self):
        app = IServiceCollection(self.app, self.app)
        return app.getServiceNamed('launcher')

    @property
    def scheduler(self):
        return self.app.getComponent(ISpiderScheduler)

    @property
    def eggstorage(self):
        return self.app.getComponent(IEggStorage)

    @property
    def poller(self):
        return self.app.getComponent(IPoller)

    @property
    def ping(self):
        return self.app.getComponent(IHostPinger, default=None)


class Home(resource.Resource):

    def __init__(self, root, local_items):
        resource.Resource.__init__(self)
        self.root = root
        self.local_items = local_items

    def render_GET(self, txrequest):
        vars = {
            'projects': ', '.join(self.root.scheduler.list_projects())
        }
        s = """
<html>
<head><title>Scrapyd Service</title></head>
<body>
<h1>Scrapyd</h1>
<p>Available projects: <b>%(projects)s</b></p>
<ul>
<li><a href="/jobs">Jobs</a></li>
""" % vars
        if self.local_items:
            s += '<li><a href="/items/">Items</a></li>'
        s += """
<li><a href="/logs/">Logs</a></li>
<li><a href="http://scrapyd.readthedocs.org/en/latest/">Documentation</a></li>
</ul>
<h2>How to schedule a spider?</h2>
<p>To schedule a spider you need to use the API (this web UI is only for
monitoring)</p>
<p>Example using <a href="http://curl.haxx.se/">curl</a>:</p>
<p><code>curl http://localhost:6800/schedule.json -d project=default -d spider=somespider</code></p>
<p>For more information about the API, see the <a href="http://scrapyd.readthedocs.org/en/latest/">Scrapyd documentation</a></p>
</body>
</html>
""" % vars
        return s.encode('utf-8')


def microsec_trunc(timelike):
    if hasattr(timelike, 'microsecond'):
        ms = timelike.microsecond
    else:
        ms = timelike.microseconds
    return timelike - timedelta(microseconds=ms)


class Jobs(resource.Resource):

    def __init__(self, root, local_items):
        resource.Resource.__init__(self)
        self.root = root
        self.local_items = local_items

    cancel_button = """
    <form method="post" action="/cancel.json">
    <input type="hidden" name="project" value="{project}"/>
    <input type="hidden" name="job" value="{jobid}"/>
    <input type="submit" style="float: left;" value="Cancel"/>
    </form>
    """.format

    header_cols = [
        'Project', 'Spider',
        'Job', 'PID',
        'Start', 'Runtime', 'Finish',
        'Log', 'Items',
        'Cancel',
    ]

    def gen_css(self):
        css = [
            '#jobs>thead td {text-align: center; font-weight: bold}',
            '#jobs>tbody>tr:first-child {background-color: #eee}',
        ]
        if not self.local_items:
            col_idx = self.header_cols.index('Items') + 1
            css.append('#jobs>*>tr>*:nth-child(%d) {display: none}' % col_idx)
        if b'cancel.json' not in self.root.children:
            col_idx = self.header_cols.index('Cancel') + 1
            css.append('#jobs>*>tr>*:nth-child(%d) {display: none}' % col_idx)
        return '\n'.join(css)

    def prep_row(self, cells):
        if not isinstance(cells, dict):
            assert len(cells) == len(self.header_cols)
        else:
            cells = [cells.get(k) for k in self.header_cols]
        cells = ['<td>%s</td>' % ('' if c is None else c) for c in cells]
        return '<tr>%s</tr>' % ''.join(cells)

    def prep_doc(self):
        return (
            '<html>'
            '<head>'
            '<title>Scrapyd</title>'
            '<style type="text/css">' + self.gen_css() + '</style>'
            '</head>'
            '<body><h1>Jobs</h1>'
            '<p><a href="..">Go up</a></p>'
            + self.prep_table() +
            '</body>'
            '</html>'
        )

    def prep_table(self):
        return (
            '<table id="jobs" border="1">'
            '<thead>' + self.prep_row(self.header_cols) + '</thead>'
            '<tbody>'
            + '<tr><th colspan="%d">Pending</th></tr>' % len(self.header_cols)
            + self.prep_tab_pending() +
            '</tbody>'
            '<tbody>'
            + '<tr><th colspan="%d">Running</th></tr>' % len(self.header_cols)
            + self.prep_tab_running() +
            '</tbody>'
            '<tbody>'
            + '<tr><th colspan="%d">Finished</th></tr>' % len(self.header_cols)
            + self.prep_tab_finished() +
            '</tbody>'
            '</table>'
        )

    def prep_tab_pending(self):
        return '\n'.join(
            self.prep_row(dict(
                Project=project, Spider=m['name'], Job=m['_job'],
                Cancel=self.cancel_button(project=project, jobid=m['_job'])
            ))
            for project, queue in self.root.poller.queues.items()
            for m in queue.list()
        )

    def prep_tab_running(self):
        return '\n'.join(
            self.prep_row(dict(
                Project=p.project, Spider=p.spider,
                Job=p.job, PID=p.pid,
                Start=microsec_trunc(p.start_time),
                Runtime=microsec_trunc(datetime.now() - p.start_time),
                Log='<a href="/logs/%s/%s/%s.log">Log</a>' % (
                    p.project, p.spider, p.job),
                Items='<a href="/items/%s/%s/%s.jl">Items</a>' % (
                    p.project, p.spider, p.job),
                Cancel=self.cancel_button(project=p.project, jobid=p.job)
            ))
            for p in self.root.launcher.processes.values()
        )

    def prep_tab_finished(self):
        def _fromat(time):
            return dateparser.parse(time)

        return '\n'.join(
            self.prep_row(dict(
                Project=p.project, Spider=p.spider,
                Job=p.job,
                Start=microsec_trunc(_fromat(p.start_time)),
                Runtime=microsec_trunc(
                    _fromat(p.start_time) - _fromat(p.end_time)),
                Finish=microsec_trunc(_fromat(p.end_time)),
                Log='<a href="/logs/%s/%s/%s.log">Log</a>' % (
                    p.project, p.spider, p.job),
                Items='<a href="/items/%s/%s/%s.jl">Items</a>' % (
                    p.project, p.spider, p.job),
            ))
            for p in self.root.launcher.finished.load()
        )

    def render(self, txrequest):
        doc = self.prep_doc()
        txrequest.setHeader('Content-Type', 'text/html; charset=utf-8')
        txrequest.setHeader('Content-Length', str(len(doc)))
        return doc.encode('utf-8')
