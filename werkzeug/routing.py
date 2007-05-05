# -*- coding: utf-8 -*-
"""
    werkzeug.routing
    ~~~~~~~~~~~~~~~~

    An extensible URL mapper.

    Map creation::

        >>> m = Map([
        ...     # Static URLs
        ...     Rule('/', endpoint='static/index'),
        ...     Rule('/about', endpoint='static/about'),
        ...     Rule('/help', endpoint='static/help'),
        ...     # Knowledge Base
        ...     Rule('/', subdomain='kb', endpoint='kb/index'),
        ...     Rule('/browse/', subdomain='kb', endpoint='kb/browse'),
        ...     Rule('/browse/<int:id>/', subdomain='kb', endpoint='kb/browse'),
        ...     Rule('/browse/<int:id>/<int:page>', subdomain='kb', endpoint='kb/browse')
        ... ], 'example.com')

    URL building::

        >>> m.build("kb/browse", dict(id=42))
        'http://kb.example.com/browse/42/'
        >>> m.build("kb/browse", dict())
        'http://kb.example.com/browse/'
        >>> m.build("kb/browse", dict(id=42, page=3))
        'http://kb.example.com/browse/42/3'
        >>> m.build("static/about")
        u'/about'
        >>> m.build("static/about", subdomain="kb")
        'http://www.example.com/about'
        >>> m.build("static/index", force_external=True)
        'http://www.example.com/'

    URL matching::

        >>> m.match("/")
        ('static/index', {})
        >>> m.match("/about")
        ('static/about', {})
        >>> m.match("/", subdomain="kb")
        ('kb/index', {})
        >>> m.match("/browse/42/23", subdomain="kb")
        ('kb/browse', {'id': 42, 'page': 23})

    Exceptions::

        >>> m.match("/browse/42", subdomain="kb")
        Traceback (most recent call last):
        ...
        werkzeug.routing.RequestRedirect: http://kb.example.com/browse/42/
        >>> m.match("/missing", subdomain="kb")
        Traceback (most recent call last):
        ...
        werkzeug.routing.NotFound: /missing
        >>> m.match("/missing", subdomain="kb")


    :copyright: 2007 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""
import sys
import re
from urlparse import urljoin
from urllib import quote_plus
try:
    set
except NameError:
    from sets import Set as set


_rule_re = re.compile(r'''
    (?P<static>[^<]*)                           # static rule data
    <
    (?:
        (?P<converter>[a-zA-Z_][a-zA-Z0-9_]*)   # converter name
        (?:\((?P<args>[^\)]*)\))?               # converter arguments
        \:                                      # variable delimiter
    )?
    (?P<variable>[a-zA-Z][a-zA-Z0-9_]*)         # variable name
    >
''', re.VERBOSE)

def parse_rule(rule):
    """
    Parse a rule and return it as generator. Each iteration yields tuples in the
    form ``(converter, arguments, variable)``. If the converter is `None` it's a
    static url part, otherwise it's a dynamic one.
    """
    pos = 0
    end = len(rule)
    do_match = _rule_re.match
    used_names = set()
    while pos < end:
        m = do_match(rule, pos)
        if m is None:
            break
        data = m.groupdict()
        if data['static']:
            yield None, None, data['static']
        variable = data['variable']
        converter = data['converter'] or 'default'
        if variable in used_names:
            raise ValueError('variable name %r used twice.' % variable)
        used_names.add(variable)
        yield converter, data['args'] or None, variable
        pos = m.end()
    if pos < end:
        remaining = rule[pos:]
        if '>' in remaining or '<' in remaining:
            raise ValueError('malformed url rule: %r' % rule)
        yield None, None, remaining


def parse_arguments(argstring, **defaults):
    """
    Helper function for the converters. It's used to parse the
    argument string and fill the defaults.
    """
    result = {}
    rest = argstring or ''

    while True:
        tmp = rest.split('=', 1)
        if len(tmp) != 2:
            break
        key, rest = tmp
        tmp = rest.split(',')
        if len(tmp) == 2:
            value, rest = tmp
        else:
            value = tmp
        key = key.strip()
        if key not in defaults:
            raise ValueError('unknown parameter %r' % key)
        conv = defaults[key][0]
        if conv is bool:
            result[key] = value.strip().lower() == 'true'
        else:
            result[key] = conv(value.strip())

    for key, value in defaults.iteritems():
        result[key] = value[1]

    return result


class RoutingException(Exception):
    """
    Special exceptions that require the application to redirect, notifies him
    about missing urls etc.
    """


class RequestRedirect(RoutingException):
    """
    Raise if the map requests a redirect. This is for example the case if
    `strict_slashes` are activated and an url that requires a leading slash.

    The attribute `new_url` contains the absolute desitination url.
    """

    def __init__(self, new_url):
        self.new_url = new_url
        RoutingException.__init__(self, new_url)


class RequestSlash(RoutingException):
    """
    Internal exception.
    """


class NotFound(RoutingException, ValueError):
    """
    Raise if there is no match for the current url.
    """


class ValidationError(ValueError):
    """
    Validation error.
    """


class Rule(object):
    """
    Represents one url pattern.
    """

    def __init__(self, string, subdomain=None, endpoint=None,
                 strict_slashes=None):
        if not string.startswith('/'):
            raise ValueError('urls must start with a leading slash')
        if string.endswith('/'):
            self.is_leaf = False
            string = string.rstrip('/')
        else:
            self.is_leaf = True
        self.rule = unicode(string)

        self.map = None
        self.strict_slashes = strict_slashes
        self.subdomain = subdomain
        self.endpoint = endpoint

        self._trace = []
        self._arguments = set()
        self._converters = {}
        self._regex = None

    def bind(self, map):
        """
        Bind the url to a map and create a regular expression based on
        the information from the rule itself and the defaults from the map.
        """
        if self.map is not None:
            raise RuntimeError('url rule %r already bound to map %r' %
                               (self, self.map))
        self.map = map
        if self.strict_slashes is None:
            self.strict_slashes = map.strict_slashes
        if self.subdomain is None:
            self.subdomain = map.default_subdomain

        regex_parts = []
        for converter, arguments, variable in parse_rule(self.rule):
            if converter is None:
                regex_parts.append(re.escape(variable))
                self._trace.append((False, variable))
            else:
                convobj = map.converters[converter](map, arguments)
                regex_parts.append('(?P<%s>%s)' % (variable, convobj.regex))
                self._converters[variable] = convobj
                self._trace.append((True, variable))
                self._arguments.add(variable)
        if not self.is_leaf:
            self._trace.append((False, '/'))

        regex = r'^<%s>%s%s$' % (
            self.subdomain == 'ALL' and '[^>]*' or re.escape(self.subdomain),
            u''.join(regex_parts),
            not self.is_leaf and '(?P<__suffix__>/?)' or ''
        )
        self._regex = re.compile(regex, re.UNICODE)

    def match(self, path):
        """
        Check if the rule matches a given path. Path is a string in the
        form ``"<subdomain>/path"`` and is assembled by the map.

        If the rule matches a dict with the converted values is returned,
        otherwise the return value is `None`.
        """
        m = self._regex.search(path)
        if m is not None:
            groups = m.groupdict()
            # we have a folder like part of the url without a trailing
            # slash and strict slashes enabled. raise an exception that
            # tells the map to redirect to the same url but with a
            # trailing slash
            if self.strict_slashes and not self.is_leaf \
               and not groups.pop('__suffix__'):
                raise RequestSlash()
            result = {}
            for name, value in groups.iteritems():
                try:
                    value = self._converters[name].to_python(value)
                except ValidationError:
                    return
                result[str(name)] = value
            return result

    def build(self, values):
        """
        Assembles the relative url for that rule. If this is not possible
        (values missing or malformed) `None` is returned.
        """
        tmp = []
        for is_dynamic, data in self._trace:
            if is_dynamic:
                try:
                    tmp.append(self._converters[data].to_url(values[data]))
                except ValidationError:
                    return
            else:
                tmp.append(data)
        return u''.join(tmp)

    def complexity(self):
        """
        The complexity of that rule.
        """
        rv = len(self._arguments)
        # a rule that listens on all subdomains is pretty low leveled.
        # below all others
        if self.subdomain == 'ALL':
            rv = -sys.maxint + rv
        return rv
    complexity = property(complexity, doc=complexity.__doc__)

    def __cmp__(self, other):
        """
        Order rules by complexity.
        """
        if not isinstance(other, Rule):
            return NotImplemented
        return cmp(other.complexity, self.complexity)

    def __unicode__(self):
        return self.rule

    def __str__(self):
        charset = self.map is not None and self.map.charset or 'utf-8'
        return unicode(self).encode(charset)

    def __repr__(self):
        if self.map is None:
            return '<%s (unbound)>' % self.__class__.__name__
        charset = self.map is not None and self.map.charset or 'utf-8'
        tmp = []
        for is_dynamic, data in self._trace:
            if is_dynamic:
                tmp.append('<%s>' % data)
            else:
                tmp.append(data)
        return '<%s %r -> %s>' % (
            self.__class__.__name__,
            u''.join(tmp).encode(charset),
            self.endpoint
        )


class BaseConverter(object):
    regex = '[^/]+'

    def __init__(self, map, args):
        self.map = map
        self.args = args

    def to_python(self, value):
        return value

    def to_url(self, value):
        return quote_plus(unicode(value).encode(self.map.charset))


class UnicodeConverter(BaseConverter):

    def __init__(self, map, args):
        super(UnicodeConverter, self).__init__(map, args)
        options = parse_arguments(args,
            minlength=(int, 0),
            maxlength=(int, -1),
            allow_slash=(bool, False)
        )
        self.minlength = options['minlength'] or None
        if options['maxlength'] != -1:
            self.maxlength = options['maxlength']
        else:
            self.maxlength = None
        if options['allow_slash']:
            self.regex = '.+?'

    def to_python(self, value):
        if (self.minlength is not None and len(value) < self.minlength) or \
           (self.maxlength is not None and len(value) > self.maxlength):
            raise ValidationError()
        return value


class IntegerConverter(BaseConverter):
    regex = '\d+'

    def __init__(self, map, args):
        super(IntegerConverter, self).__init__(map, args)
        options = parse_arguments(args,
            fixed_digits=(int, -1),
            min=(int, 0),
            max=(int, -1)
        )
        self.fixed_digits = options['fixed_digits']
        self.min = options['min'] or None
        if options['max'] == -1:
            self.max = None
        else:
            self.max = options['max']

    def to_python(self, value):
        if (self.fixed_digits != -1 and len(value) != self.fixed_digits):
            raise ValidationError()
        value = int(value)
        if (self.min is not None and value < self.min) or \
           (self.max is not None and value > self.max):
            raise ValidationError()
        return value


class Map(object):
    """
    The base class for all the url maps.
    """
    converters = {
        'int':          IntegerConverter,
        'string':       UnicodeConverter,
        'default':      UnicodeConverter
    }

    def __init__(self, rules, server_name=None, default_subdomain='www',
                 url_scheme='http', charset='utf-8', strict_slashes=True):
        """
        `rules`
            sequence of url rules for this map.

        `server_name`
            hostname of the server excluding any subdomains but with
            the tld. Must not contain non ascii chars, if you want to
            use a i18n domain name you have to provide the domain name
            encoded in punycode.

        `default_subdomain`
            The default subdomain for rules without a subdomain defined.

        `url_scheme`
            url scheme. For example ``"http"`` or ``"https"``.

        `charset`
            charset of the url. defaults to ``"utf-8"``

        `strict_slashes`
            Take care of trailing slashes.
        """
        self._rules = []
        self._rules_by_endpoint = {}
        self._remap = True

        self.server_name = server_name
        self.default_subdomain = default_subdomain
        self.url_scheme = url_scheme
        self.charset = charset
        self.strict_slashes = strict_slashes

        for rule in rules:
            self.add_rule(rule)

    def add_rule(self, rule):
        """
        Add a new rule to the map and bind it. Requires that the rule is
        not bound to another map. After adding new rules you have to call
        the `remap` method.
        """
        if not isinstance(rule, Rule):
            raise TypeError('rule objects required')
        rule.bind(self)
        self._rules.append(rule)
        self._rules_by_endpoint.setdefault(rule.endpoint, []).append(rule)
        self._remap = True

    def match(self, path_info, script_name='/', subdomain=None):
        """
        Match a given path_info, script_name and subdomain against the
        known rules. If the subdomain is not given it defaults to the
        default subdomain of the map which is usally `www`. Thus if you
        don't defined it anywhere you can safely ignore it.
        """
        if self._remap:
            self._remap = False
            self._rules.sort()
        if subdomain is None:
            subdomain = self.default_subdomain
        if not script_name.endswith('/'):
            script_name += '/'
        if not isinstance(path_info, unicode):
            path_info = path_info.decode(self.charset, 'ignore')
        path = u'<%s>/%s' % (subdomain, path_info.lstrip('/'))
        for rule in self._rules:
            try:
                rv = rule.match(path)
            except RequestSlash:
                raise RequestRedirect(str('%s://%s%s%s/%s/' % (
                    self.url_scheme,
                    subdomain and subdomain + '.' or '',
                    self.server_name,
                    script_name[:-1],
                    path_info.lstrip('/')
                )))
            if rv is not None:
                return rule.endpoint, rv
        raise NotFound(path_info)

    def build(self, endpoint, values=None, script_name='/', subdomain=None,
              force_external=False):
        """
        Build a new url hostname relative to the current one. If you
        reference a resource on another subdomain the hostname is added
        automatically. You can force external urls by setting
        `force_external` to `True`.
        """
        if self._remap:
            self._remap = False
            self._rules.sort()
        if subdomain is None:
            subdomain = self.default_subdomain
        if not script_name.endswith('/'):
            script_name += '/'
        possible = set(self._rules_by_endpoint.get(endpoint) or ())
        if not possible:
            raise NotFound(endpoint)
        values = values or {}
        valueset = set(values.iterkeys())
        for rule in possible:
            if rule._arguments == valueset:
                rv = rule.build(values)
                if rv is not None:
                    break
        else:
            raise NotFound(endpoint, values)
        if not force_external and rule.subdomain == subdomain:
            return unicode(urljoin(script_name, rv.lstrip('/')))
        return str('%s://%s.%s%s/%s' % (
            self.url_scheme,
            rule.subdomain,
            self.server_name,
            script_name[:-1],
            rv.lstrip('/')
        ))
