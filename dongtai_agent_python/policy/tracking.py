import os
import re
import sys
import traceback

from pkg_resources import parse_version
# from regexploit import redos
# from regexploit.ast.sre import SreOpParser

from dongtai_agent_python import CONTEXT_TRACKER
from dongtai_agent_python.setting import const
from dongtai_agent_python.utils import scope, utils


class Tracking(object):
    def __init__(self, policy_rule):
        self.context = CONTEXT_TRACKER.current()
        self.ignore_tracking = False

        self.policy_rule = policy_rule

        self.caller_class = ''
        self.caller_line_number = ''
        self.caller_method = ''

    @scope.scope(scope.SCOPE_AGENT)
    def get_caller(self, layer):
        tracert = traceback.extract_stack()
        tracert_arr = list(tracert[layer])
        path = sys.path[0]

        not_direct_invoke = [
            'flask.app.Flask.make_response',
            'django.urls.resolvers.RoutePattern.match',
        ]
        while layer > -20:
            tracert_arr = list(tracert[layer])
            if self.policy_rule.signature in not_direct_invoke:
                break

            if path in tracert_arr[0] and \
                    (path + os.sep + "dongtai_agent_python") not in tracert_arr[0] and \
                    ("site-packages" + os.sep + "dongtai_agent_python") not in tracert_arr[0]:
                break
            layer = layer - 1

        # bypass some indirect call stack
        if self.policy_rule.signature not in not_direct_invoke and path not in tracert_arr[0]:
            self.ignore_tracking = True
            return

        # verify xml parser for xxe
        lxml_checks = [
            "lxml.etree.fromstring",
            "lxml.etree.parse",
        ]
        if self.policy_rule.signature in lxml_checks and tracert_arr[3]:
            if re.search('''XMLParser\\([^)]*resolve_entities\\s*=\\s*False[^)]*\\)''', tracert_arr[3]):
                self.ignore_tracking = True
                return

        self.caller_class = tracert_arr[0]
        self.caller_line_number = tracert_arr[1]
        self.caller_method = tracert_arr[2]

    @scope.scope(scope.SCOPE_AGENT)
    def apply(self, self_obj, result, args, kwargs):
        if (self.policy_rule.signature == 'yaml.load' or self.policy_rule.signature == 'yaml.load_all') and \
                yaml_load_is_safe(args, kwargs):
            return

        source = self.policy_rule.get_source_taints(self_obj, result, args, kwargs)
        # @TODO: improve performance
        source_ids = recurse_tracking(source, self.policy_rule.node_type)

        if self.policy_rule.node_type != const.NODE_TYPE_SOURCE:
            if self.policy_rule.signature in const.CRYPTO_BAD_CIPHER_NEW:
                pass
            elif (self.policy_rule.signature.startswith('Crypto.Cipher._mode_') or
                  self.policy_rule.signature.startswith('Cryptodome.Cipher._mode_')) and \
                    self.policy_rule.signature.endswith('Mode.encrypt'):
                for sid in source_ids:
                    if sid not in self.context.taint_ids:
                        return
            elif len(list(set(self.context.taint_ids) & set(source_ids))) == 0:
                return

        if self.policy_rule.signature.startswith('re.'):
            if re_is_safe(self.context.taint_ids, args, kwargs):
                return

        self.get_caller(-4)
        if self.ignore_tracking:
            return

        source_arr = []
        for src in source:
            try:
                source_arr.append(str(src))
            except Exception:
                continue

        target = self.policy_rule.get_target_taints(self_obj, result, args, kwargs)
        target_ids = recurse_tracking(target, self.policy_rule.node_type)
        for target_id in target_ids:
            if target_id not in self.context.taint_ids:
                self.context.taint_ids.append(target_id)

        if len(target) == 1:
            target_values = target[0]
        else:
            target_values = target

        pool = {
            "invokeId": len(self.context.pool) + 1,
            "interfaces": [],
            "targetHash": target_ids,
            "targetValues": str(target_values),
            "signature": self.policy_rule.signature,
            "originClassName": self.policy_rule.fully_class_name,
            "sourceValues": str(source),
            "methodName": self.policy_rule.method_name,
            "className": self.policy_rule.fully_class_name,
            "source": self.policy_rule.node_type == const.NODE_TYPE_SOURCE,
            "callerLineNumber": self.caller_line_number,
            "callerClass": self.caller_class,
            "args": "",
            "callerMethod": self.caller_method,
            "sourceHash": source_ids,
            "retClassName": ""
        }

        self.context.pool.append(pool)


@scope.with_scope(scope.SCOPE_AGENT)
def yaml_load_is_safe(args, kwargs=None):
    if kwargs is None:
        kwargs = {}

    try:
        import yaml
        if parse_version(yaml.__version__) < parse_version('5.1'):
            if len(args) < 2 and 'Loader' not in kwargs:
                return False
    except ImportError:
        pass

    if len(args) == 2:
        if args[1].__name__ == 'UnsafeLoader':
            return False
    if 'Loader' in kwargs:
        if kwargs['Loader'].__name__ == 'UnsafeLoader':
            return False
    return True


@scope.with_scope(scope.SCOPE_AGENT)
def re_is_safe(taint_ids, args, kwargs=None):
    if args is None:
        args = ()
    if kwargs is None:
        kwargs = {}

    if len(args) > 0:
        pattern = args[0]
    elif 'pattern' in kwargs:
        pattern = kwargs['pattern']
    else:
        return True

    if not pattern:
        return True

    if utils.get_hash(pattern) in taint_ids:
        return False

    # @TODO: static analysis
    """
    if isinstance(pattern, const.EMPTY_PATTERN):
        pattern = pattern.pattern

    try:
        parsed = SreOpParser().parse_sre(pattern)
    except re.error:
        return True

    for rd in redos.find(parsed):
        if rd.starriness > 2:
            return False
    """

    return True


@scope.with_scope(scope.SCOPE_AGENT)
def recurse_tracking(obj, node_type, hash_ids=None):
    if obj is None:
        return []

    if hash_ids is None:
        hash_ids = []

    for item in obj:
        if utils.is_empty(item) or utils.is_not_allowed_type(item):
            continue

        try:
            item_type = ".".join([type(item).__module__, type(item).__name__])
        except Exception:
            item_type = ''

        hid = utils.get_hash(item)
        if hid not in hash_ids:
            hash_ids.append(hid)

        if isinstance(item, (tuple, list)):
            hash_ids = recurse_tracking(item, node_type, hash_ids)
        elif isinstance(item, dict):
            hash_ids = recurse_tracking(list(item.values()), node_type, hash_ids)
        elif item_type == 'django.template.context.RequestContext' or \
                item_type == 'django.template.context.Context':
            for it in item:
                hash_ids = recurse_tracking([it], node_type, hash_ids)

    return hash_ids
