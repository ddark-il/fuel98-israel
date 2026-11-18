#!/usr/bin/env python3
"""Live client for Waze's rt distributor - reverse-engineered from Waze 5.24.5.0.

WHAT WORKS (verified against production, 2026-10-02)
    transport   POST https://rt.waze.com/rtserver/distrib/command
                body: "ProtoBase64," + base64( linqmap.proto.rt.Batch )
                resp: protobuf TextFormat or binary Batch
    register    Element.register = 2219  -> RegisterSuccessful{username,password,token,user_id}
                                          + client_auth_token  (a real anonymous account)
    auth        Element.authenticate = 2338 (legacy) -> old_command
                "AuthenticateSuccessful,<uid>,<base64 token>"

WHERE IT STOPS
    Element.uid = 2221 (linqmap.proto.rt.UID{id, secret_key, protocol}) is required by every
    "bridge" command (venue_search, search_config, report_ads_setting...). The server checks it:
        no secret_key -> "secretKey missing"
        unknown pair  -> "Unknown userid/userid = N. Client should relogin to continue working."
    A valid (id, secret_key) pair is only issued by a successful login, and every login variant
    is rejected with LoginError/internal_issues_details{issue_type: MISSING_MANDATORY_PROPERTY}.
    Details and open leads: LIVE_ACCESS.md

USAGE
    python3 waze_live.py register              # create + save an anonymous account
    python3 waze_live.py auth                  # legacy authenticate, show uid/secret
    python3 waze_live.py uid                   # probe the UID element with what we have
    python3 waze_live.py config                # try the fuel-type catalog (needs a session)
    python3 waze_live.py search --lat --lon    # try a venue search    (needs a session)
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'tools'))
import pbtext                                                   # noqa: E402

import importlib.util                                           # noqa: E402
_spec = importlib.util.spec_from_file_location('poc', os.path.join(_HERE, 'waze_rt_poc.py'))
poc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poc)

# The distributor has one endpoint for the whole world and a **regional proxy per cluster**, and the
# two are not equivalent: `rt.waze.com` load-balances every request across the `-il-` and `-row-`
# frontends, and only the Israeli one is attached to the service mesh that carries fuel prices
# (`venue.prod.il.mesh-waze`, named in a bridge error). Measured over 6 draws: rt.waze.com -> 5 row /
# 1 il, rtproxy-il.waze.com -> 6 il, all with prices. The name comes from the app's own server list
# and from the SAN list of its certificate (which also carries the legacy `rt-il.waze.com`, still
# serving but with a certificate that expired in 2023).
BASE = os.environ.get('WAZE_BASE') or 'https://rtproxy-il.waze.com/rtserver'

UA = ('Waze/5.24.5.0 (com.waze; Android 13; he_IL) Mozilla/5.0 (Linux; Android 13; '
      'sdk_gphone64_arm64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 '
      'Mobile Safari/537.36')
ACCOUNT_FILE = 'waze_account.json'
AFFINITY_COOKIE = 'Waze-Session-Affinity'

# response-side element numbers
EL_RESPONSE_TIMESTAMP = 2150
EL_OLD_COMMAND = 2100
EL_MAP = {
    poc.EL_ERROR: 'error',
    EL_RESPONSE_TIMESTAMP: 'response_timestamp',
    EL_OLD_COMMAND: 'old_command',
    poc.EL_REGISTER_SUCCESSFUL: 'register_successful',
    poc.EL_REGISTER_ERROR: 'register_error',
    poc.EL_CLIENT_AUTH_TOKEN: 'client_auth_token',
    poc.EL_LOGIN_RESPONSE: 'login_response',
    poc.EL_LOGIN_SUCCESSFUL: 'login_successful',
    poc.EL_LOGIN_ERROR: 'login_error',
    poc.EL_SEARCH_CONFIG_RESPONSE: 'search_config_response',
    poc.EL_VENUE_LIST: 'venue_list',
    poc.EL_SEARCH_V2_RESPONSE: 'search_v2_response',
    poc.EL_SEARCH_RESPONSE: 'search_response',
    poc.EL_VENUE_STATUS_RESPONSE: 'venue_status_response',
}


# --------------------------------------------------------------------- decode
def _flat(blob: bytes) -> dict:
    return {str(k): [v for _w, v in vals] for k, vals in poc.pb_decode(blob).items()}


def _val(v):
    if isinstance(v, bytes):
        try:
            s = v.decode('utf-8')
            if all(c.isprintable() or c == '\n' for c in s):
                return s
        except UnicodeDecodeError:
            pass
        return _flat(v)
    return v


def _login_response(blob: bytes) -> dict:
    f = poc.pb_decode(blob)
    out = {}
    if 1 in f:                                   # LoginResponse.login_success
        out['login_success'] = _flat(f[1][0][1])
    if 2 in f:                                   # LoginResponse.login_error
        err = poc.pb_decode(f[2][0][1])
        d = {'error_type': err.get(2, [(0, None)])[0][1]}
        issues = err.get(5)
        if issues:
            d['issue_type'] = _flat(issues[0][1]).get('1', [None])[0]
        out['login_error'] = d
    return out or _flat(blob)


def decode(body: bytes) -> dict:
    """Response -> {'element': [ {name: value | [value, ...]}, ... ]} in TextFormat shape."""
    text = body.decode('utf-8', 'replace')
    if text.lstrip().startswith(('element', 'Element')):
        return pbtext.parse(text)
    out = []
    for _w, el in poc.pb_decode(body).get(1001, []):
        item = {}
        for num, vals in poc.pb_decode(el).items():
            name = EL_MAP.get(num, f'element_{num}')
            for wire, v in vals:
                if name == 'error':
                    item[name] = {k: _val(x) for k, x in _flat(v).items()}
                elif name == 'old_command':
                    item[name] = v.decode('utf-8', 'replace').strip()
                elif name == 'login_response':
                    item[name] = _login_response(v)
                elif name == 'response_timestamp':
                    item[name] = {k: _val(x) for k, x in _flat(v).items()}
                elif wire == 2:
                    item[name] = _flat(v)
                else:
                    item[name] = v
        out.append(item)
    return {'element': out}


def element_with(resp: dict, key: str):
    for el in resp.get('element', []):
        if key in el:
            v = el[key]
            return v[0] if isinstance(v, list) and v and not isinstance(v[0], dict) else v
    return None


def show(resp: dict, label: str = '') -> None:
    if label:
        print(f'== {label}')
    for el in resp.get('element', []):
        for k, v in el.items():
            if k == 'response_timestamp':
                continue
            if isinstance(v, list):
                txt = ' | '.join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
                                 for x in v)
            elif isinstance(v, str):
                txt = v
            else:
                txt = json.dumps(v, ensure_ascii=False)
            print(f'   {k}: {txt[:400]}')


# -------------------------------------------------------------------- session
def prefer_ipv4() -> None:
    """Pin the API to its A record.

    rt.waze.com publishes an AAAA as well (Google's *global* IPv6 anycast). From a host with IPv6
    that is the address python picks, and the edge it lands on answers searches normally while
    returning **no fuel products at all** - which is exactly how a Waze run looks when it is
    "blocked" but has no error to show: the same account and the same 75 stations gave 0 prices on
    a GitHub runner and prices from Israel. Opt out with WAZE_IPV6=1.
    """
    if os.environ.get('WAZE_IPV6') == '1' or getattr(socket.getaddrinfo, '_waze_ipv4', False):
        return
    orig = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family in (0, socket.AF_UNSPEC) and not str(host).startswith('['):
            try:
                v4 = orig(host, port, socket.AF_INET, type or socket.SOCK_STREAM, proto, flags)
                if v4:
                    return v4
            except OSError:
                pass                                  # no A record: fall back to whatever exists
        return orig(host, port, family, type, proto, flags)

    getaddrinfo._waze_ipv4 = True
    socket.getaddrinfo = getaddrinfo          # type: ignore[assignment]


class Session:
    def __init__(self, country: str = 'IL', env: str = 'il', verbose: bool = False):
        self.country, self.env, self.verbose = country, env, verbose
        prefer_ipv4()
        self.sessionid = -1
        # WAZE_SESSION_COOKIE lets a machine that cannot reach an IL frontend start from one that a
        # machine in Israel obtained. Empty by default: a normal run mints its own.
        self.affinity = os.environ.get('WAZE_SESSION_COOKIE', '')
        self.cookie = ''
        self.rtserver_id = None
        self.installation_id = '00000000-0000-4000-8000-000000000000'
        self.device_id = '0000000000000000'
        self.account: dict | None = None
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.last_headers: dict = {}

    # -- transport ---------------------------------------------------------
    def params(self, extra: dict | None = None) -> dict:
        p = {'sessionid': str(self.sessionid), 'cookie': self.cookie or self.affinity,
             'client_version': '5.24.5.0', 'env': self.env}
        if self.rtserver_id:
            p['rtserver-id'] = self.rtserver_id
        p.update(extra or {})
        return p

    def request_url(self, path: str = '/command', params: dict | None = None) -> str:
        return BASE + path + '?' + urlencode(self.params(params))

    def _absorb_headers(self, headers: dict) -> None:
        self.last_headers = headers
        sc = headers.get('set-cookie') or headers.get('Set-Cookie') or ''
        if 'rtserver-id=' in sc:
            self.rtserver_id = sc.split('rtserver-id=')[1].split(';')[0]
        if AFFINITY_COOKIE in sc:
            # `Waze-Session-Affinity` is the distributor's own stickiness token: the frontend that
            # serves a request hands it out (an IL frontend did, with the fuel prices attached), and
            # a client that keeps it is kept on that frontend - this is the `cookie=` the app puts in
            # every URL (`nativeManager.getServerCookie()`), and the reason the app does not depend on
            # the draw-by-draw routing lottery we hit. Store it, send it back.
            self.affinity = sc.split(AFFINITY_COOKIE + '=')[1].split(';')[0].strip('"')

    def post_keepalive(self, conn, batch: bytes, path: str = '/command') -> dict:
        """POST over a caller-owned connection, keeping it open.

        The distributor edge assigns a backend **per connection** - one that lasts: ten requests
        over one kept-alive connection all came back from the same cluster, while ten separate
        connections split between them. A connection that answers from `...-il-*` is therefore worth
        holding on to: it carries the Israeli fuel prices, and every request sent over it does too.
        """
        body = b'ProtoBase64,' + base64.b64encode(batch)
        hdrs = {'Content-Type': 'text/plain', 'User-Agent': UA, 'Connection': 'keep-alive'}
        if self.affinity:
            hdrs['Cookie'] = f'{AFFINITY_COOKIE}="{self.affinity}"'
        conn.request('POST', self.request_url(path), body=body, headers=hdrs)
        r = conn.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in (r.getheaders() or [])}
        hdrs.setdefault('status', str(r.status))
        self._absorb_headers(hdrs)
        return decode(data)

    def post(self, batch: bytes, path: str = '/command', params: dict | None = None,
             raw_out: str | None = None) -> dict:
        body = b'ProtoBase64,' + base64.b64encode(batch)
        url = self.request_url(path, params)
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'text/plain')
        req.add_header('User-Agent', UA)
        if self.affinity:
            req.add_header('Cookie', f'{AFFINITY_COOKIE}="{self.affinity}"')
        try:
            with self.opener.open(req, timeout=45) as r:
                status, headers, data = r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            status, headers, data = e.code, dict(e.headers), e.read()
        if raw_out:
            open(raw_out, 'wb').write(data)
        self._absorb_headers(headers)
        resp = decode(data)
        if self.verbose:
            print(f'   -> {url}\n   <- HTTP {status} {len(data)}B '
                  f'x-waze-error-code={headers.get("x-waze-error-code")}', file=sys.stderr)
        return resp

    # -- request pieces ----------------------------------------------------
    def client_info(self, with_name: bool = True) -> bytes:
        extra = poc.pb_str(7, self.account['username']) if (with_name and self.account) else b''
        return poc.client_info(self.installation_id, self.device_id) + extra

    def send(self, *extra_elements: bytes, **kw) -> dict:
        els = [poc.element(**{str(poc.EL_CLIENT_INFO): self.client_info()})]
        els.extend(extra_elements)
        return self.post(poc.batch(*els), **kw)

    def uid_element(self, uid: int, secret: str, protocol: int = 4) -> bytes:
        return poc.element(**{str(2221): poc.pb_int(1, uid) + poc.pb_str(2, secret)
                              + poc.pb_int(3, protocol)})

    def venue_search_element(self, lat: float, lon: float, max_results: int = 25) -> bytes:
        return poc.element(**{str(poc.EL_VENUE_SEARCH_REQUEST):
                              poc.venue_search(lat, lon, '', max_results)})

    def search_config_element(self) -> bytes:
        return poc.element(**{str(poc.EL_SEARCH_CONFIG_REQUEST):
                              poc.search_config_request(self.country)})

    # -- flows -------------------------------------------------------------
    def register(self) -> dict:
        return self.send(poc.element(**{str(poc.EL_REGISTER): poc.register_payload()}))

    def save_account(self, resp: dict, path: str = ACCOUNT_FILE) -> dict | None:
        def unwrap(m, key):
            v = m.get(key) if isinstance(m, dict) else None
            while isinstance(v, list) and v:      # TextFormat gives [value] / [{...}]
                v = v[0]
            return v

        rs = element_with(resp, 'register_successful')
        while isinstance(rs, list) and rs:
            rs = rs[0]
        tok = element_with(resp, 'client_auth_token')
        while isinstance(tok, list) and tok:
            tok = tok[0]
        if not isinstance(rs, dict) or not unwrap(rs, 'username'):
            return None                            # keep the previous state file untouched
        acct = {'username': unwrap(rs, 'username'), 'password': unwrap(rs, 'password'),
                'token': unwrap(rs, 'token') or unwrap(tok, 'token'),
                'user_id': unwrap(rs, 'user_id'),
                'installation_id': self.installation_id}
        json.dump(acct, open(path, 'w'), indent=1)
        self.account = acct
        self.cookie = acct.get('token') or ''
        return acct

    def load_account(self, path: str = ACCOUNT_FILE) -> bool:
        try:
            self.account = json.load(open(path))
        except Exception:
            return False
        self.installation_id = self.account.get('installation_id', self.installation_id)
        self.cookie = self.account.get('token') or ''
        return bool(self.cookie)

    def authenticate(self) -> dict:
        """Legacy authenticate: returns {'uid':.., 'token':.., 'secret':..}"""
        a = self.account or {}
        el = poc.element(**{str(2338): poc.pb_int(1, 4) + poc.pb_str(2, a.get('username', ''))
                            + poc.pb_str(3, a.get('password', ''))})
        resp = self.send(el)
        out = {}
        cmds = []
        for el in resp.get('element', []):
            v = el.get('old_command')
            if v is None:
                continue
            for c in (v if isinstance(v, list) else [v]):
                cmds.append(c.strip() if isinstance(c, str) else str(c))
        for c in cmds:
            if c.startswith('AuthenticateSuccessful'):
                _, uid, tok = c.split(',')[:3]
                tok = tok.strip()
                out['uid'] = int(uid)
                out['token'] = tok
                try:
                    raw = base64.b64decode(tok + '=' * (-len(tok) % 4))
                    inner = poc.pb_decode(raw)
                    if 1 in inner:
                        out['secret'] = inner[1][0][1].decode()
                    if 2 in inner:
                        out['big'] = inner[2][0][1]
                    if 3 in inner:
                        out['third'] = inner[3][0][1]
                except Exception:
                    pass
        out['raw'] = cmds
        return out


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description='Live Waze rt client (see LIVE_ACCESS.md)')
    ap.add_argument('command', choices=['register', 'auth', 'uid', 'config', 'search'])
    ap.add_argument('--lat', type=float, default=32.0853)
    ap.add_argument('--lon', type=float, default=34.7818)
    ap.add_argument('--country', default='IL')
    ap.add_argument('--account', default=ACCOUNT_FILE)
    ap.add_argument('--fresh-account', action='store_true')
    ap.add_argument('--raw-out', default=None)
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()

    s = Session(country=a.country, verbose=a.verbose)
    have = s.load_account(a.account)
    if a.command == 'register' or a.fresh_account or not have:
        acct = s.save_account(s.register())
        print('anonymous account:', json.dumps(acct, ensure_ascii=False))
        if a.command == 'register':
            return 0
    else:
        print(f"account: {a.account} user_id={s.account.get('user_id')}")

    if a.command == 'auth':
        show(s.send(poc.element(**{str(2338): poc.pb_int(1, 4)
                                   + poc.pb_str(2, s.account['username'])
                                   + poc.pb_str(3, s.account['password'])})), 'authenticate')
        return 0

    if a.command == 'uid':
        info = s.authenticate()
        print('authenticate ->', {k: v for k, v in info.items() if k != 'raw'})
        for label, um in {
            'uid{legacy id + secret}': (info.get('uid'), info.get('secret')),
            'uid{registered id + token}': (s.account.get('user_id'), s.account.get('token')),
        }.items():
            if not um[0]:
                continue
            resp = s.send(s.uid_element(um[0], um[1] or ''),
                          s.venue_search_element(a.lat, a.lon, 5))
            show(resp, label)
        return 0

    if a.command == 'config':
        show(s.send(s.search_config_element(), raw_out=a.raw_out), 'search_config')
        return 0

    if a.command == 'search':
        show(s.send(s.venue_search_element(a.lat, a.lon), raw_out=a.raw_out),
             f'venue_search @ {a.lat},{a.lon}')
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
