import base64
import hashlib
import datetime

import msgpack

from utils import dictargs, utcnow
import config
from key import key

ALIAS_ACCESS = "alias/access"
ALIAS_AUTHZ = "alias/authorize"
ALIAS_BIND = "alias/bind"
ALIAS_CERT = "alias/crt"
ALIAS_REFRESH = "alias/refresh"
ALIAS_REGISTER = "alias/register"
ALIAS_REVOKE = "alias/revoke"
ALIAS_SUBKEY = "alias/key"

hasher = getattr(hashlib, config.DEFAULT_HASH)


class BaseException(Exception):
    REASON = None

    def __init__(self, o):
        self.o = o

        super().__init__(self.reason())

    def reason(self):
        return f"{self.REASON}: {format(self.o)}"


class FutureSignatureException(BaseException):
    REASON = "order is signed in the future"

    def __init__(self, o, delta):
        self.delta = delta
        super().__init__(o)

    def reason(self):
        return super().reason() + f": {datetime.timedelta(seconds=self.delta)}"


class FutureSignatureReferenceException(FutureSignatureException):
    REASON = "referenced order is signed in the future"


class AlreadySignedException(BaseException):
    REASON = "order is already signed"


class InvalidSignatureException(BaseException):
    REASON = "invalid signature"


class ExpiredSignatureException(BaseException):
    REASON = "expired signature"


class ExpiredOrderException(BaseException):
    REASON = "expired order"


class ExpiredOrderReferenceException(ExpiredOrderException):
    REASON = "expired order reference"


class RevokedOrderException(BaseException):
    REASON = "revoked order"


def new(type_, **kwargs):
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    assert 'type' not in kwargs
    kwargs['type'] = type_
    return kwargs


def signed(o):
    return '_sig' in o


def root_hash(x):
    # if isinstance(o, RootHash):
    #   XXX

    if isinstance(x, str):
        return hasher(x.encode('utf-8')).digest()

    elif isinstance(x, bytes):
        return root_hash(base64.b64encode(x).decode('ascii'))

    elif isinstance(x, (int, float)):
        return root_hash(str(x))

    elif isinstance(x, (list, tuple)):
        h_idx, h_val = hasher(), hasher()

        for idx, v in enumerate(x):
            h_idx.update(root_hash(idx))
            h_val.update(root_hash(v))

        return hasher(h_idx.digest() + h_val.digest()).digest()

    elif isinstance(x, dict):
        if signed(x):
            # pop signature
            o_ = dict(x)
            sig_o = dict(o_.pop('_sig'))
            sig_o['order'] = o_

            assert not signed(sig_o)
            return root_hash(sig_o)

        else:
            h_key, h_val = hasher(), hasher()

            for k, v in sorted(x.items()):
                h_key.update(root_hash(k))
                h_val.update(root_hash(v))

            return hasher(h_key.digest() + h_val.digest()).digest()

    elif x is None:
        return bytes(hasher().digest_size)

    else:
        raise TypeError(type(x))


def sign(o, sk, k=None, exp=None, now=None, store=None):
    if '_sig' in o:
        raise AlreadySignedException(o)

    if now is None:
        now = utcnow().timestamp()

    if k is None:
        k = sk.public().to_dict()

    # Ensure key is valid
    check(k, store=store, now=now)

    sig = dictargs(
        k=k,
        dat=now,
        exp=exp,
    )

    proof = _Signature(sig, o).sign(sk)
    sig['proof'] = proof
    o['_sig'] = sig
    assert signed(o)

    if store:
        store.store_order(o)

    else:
        print("DEBUG: order signed without being stored!")

    return o


def _pack_default(x):
    if isinstance(x, _Signature):
        m = _pack([x.sig, x.order, x.proof])
        return msgpack.ExtType(_Signature.MSGPACK_EXT_ID, m)
    raise TypeError(type(x))


def _pack(x):
    return msgpack.packb(x, default=_pack_default, use_bin_type=True)


def _unpack_ext_hook(code, data):
    if code == _Signature.MSGPACK_EXT_ID:
        sig, order, proof = _unpack(data)

        if not _Signature(sig, order, proof).verify():
            raise InvalidSignatureException()

        sig['proof'] = proof
        order['_sig'] = sig
        return order

    return msgpack.ExtType(code, data)


def _unpack(x):
    return msgpack.unpackb(x, ext_hook=_unpack_ext_hook, raw=False)


class _Signature:
    MSGPACK_EXT_ID = 1

    def __init__(self, sig, order, proof=None):
        assert '_sig' not in order
        assert 'proof' not in sig

        self.sig = sig
        self.order = order
        self.proof = proof

    def _pack(self):
        return _pack(_raw(self.sig)) + b'.' + _pack(_raw(self.order))

    def sign(self, sk):
        self.proof = sk.sign(self._pack())
        return self.proof

    def verify(self):
        return key.from_dict(self.sig['k']).verify(self.proof, self._pack())


def _raw(x):
    if x is None or isinstance(x, (str, bytes, int, float)):
        return x

    elif isinstance(x, (list, tuple)):
        return [_raw(i) for i in x]

    elif isinstance(x, dict):
        if signed(x):
            o_ = dict(x)
            sig_ = dict(o_.pop('_sig'))
            proof = sig_.pop('proof')

            # check signature
            sig = _Signature(sig_, o_, proof)
            if not sig.verify():
                raise InvalidSignatureException()

            return sig

        else:
            return {k: _raw(v) for k, v in x.items()}

    else:
        raise TypeError(type(x))


def to_raw(x):
    return _pack(_raw(x))


def to_token(x):
    return base64.urlsafe_b64encode(to_raw(x)).decode('ascii')


def from_raw(x):
    return _unpack(x)


def from_token(x, auto_check=None, store=None):
    if auto_check is None:
        auto_check = True

    if auto_check and not store:
        print("DEBUG: token is parsed without revocation store!")

    o = from_raw(base64.urlsafe_b64decode(x.encode('ascii')))

    if auto_check:
        check(o, store=store)

    return o


def iter_signed_parents(o, parent=None):
    if isinstance(o, (tuple, list)):
        for i in o:
            yield from iter_signed_parents(i, parent)

    elif isinstance(o, dict):
        child_parent = o if signed(o) else parent

        for i in o.values():
            yield from iter_signed_parents(i, child_parent)

        if signed(o):
            yield o, parent


def iter_signed(o):
    return (i[0] for i in iter_signed_parents(o))


def check(o, store=None, now=None):
    assert o
    if now is None:
        now = utcnow().timestamp()

    def assert_or_raise(x, exc_cls, o_, *kargs):
        if not x:
            raise exc_cls(o_, *kargs)

    orders = list(iter_signed_parents(o))

    for oi, parent_o in orders:
        # check signature is not in the future
        nbf = oi['_sig']['dat']
        assert_or_raise(now >= nbf, FutureSignatureException, oi, nbf - now)

        # check parent order didn't sign an order signed in the future
        if parent_o:
            assert_or_raise(parent_o['_sig']['dat'] >= nbf, FutureSignatureReferenceException, parent_o, nbf - parent_o['_sig']['dat'])

        # check no signatures expired
        sig_exp = oi['_sig'].get('exp')
        if sig_exp:
            assert_or_raise(now < nbf + sig_exp, ExpiredSignatureException, oi)

        o_exp = oi.get('exp')
        if o_exp:
            o_naf = nbf + o_exp

            # if oi is root, check not expired
            if parent_o is None:
                assert_or_raise(now < o_naf, ExpiredOrderException, oi)

            # if oi is not root, check no order was expired when referenced by a parent signed order
            else:
                assert_or_raise(parent_o['_sig']['dat'] < o_naf, ExpiredOrderReferenceException, parent_o)

    # check no orders were revoked
    if store:
        orders_h = [root_hash(oi) for oi, _ in orders]
        assert_or_raise(not store.bulk_is_revoked(orders_h), RevokedOrderException, o)


def expiration(o):
    """ Returns order's expiration date. Returned value if order is not valid. """

    def iter_expirations(o):
        # signatures expiration
        for o, parent_o in iter_signed_parents(o):
            if 'exp' in o['_sig']:
                yield o['_sig']['dat'] + o['_sig']['exp']

            if 'exp' in o and parent_o is None:
                yield o['_sig']['dat'] + o['exp']

    nafs = list(iter_expirations(o))
    if nafs:
        min_naf = min(nafs)
        return datetime.datetime.utcfromtimestamp(min_naf)
    else:
        return None


def format(o):
    from copy import deepcopy
    from pprint import pformat

    if isinstance(o, dict) and signed(o):
        signer = root_signer(o)
        date = datetime.datetime.utcfromtimestamp(o['_sig']['dat'])
        naf = expiration(o)

        o_ = deepcopy(o)
        o_.pop("_sig")
        o_.pop("type")

        # replace all signatures
        def walk(o):
            if isinstance(o, (list, tuple)):
                return [walk(i) for i in o]
            elif isinstance(o, dict):
                if signed(o):
                    o.pop('_sig')
                    o['_is_signed'] = True

                return {k: walk(v) for k, v in o.items()}
            else:
                return o

        o_ = walk(o_)

        r = [f"order '{o['type']}' "]
        r.append(f"(signer={signer} @ {date}")
        if naf:
            r.append(f", naf {naf}")
        r.append(f"): {pformat(o_)}")
        return "".join(r)

    return repr(o)


def root_signer(o):
    if not signed(o):
        return None

    x = o
    while '_sig' in x:
        x = x['_sig']['k']

    return key.from_dict(x)


def parents(o, is_root=None):
    if is_root is None:
        is_root = True
    assert isinstance(is_root, bool)

    if isinstance(o, (tuple, list)):
        for i in o:
            yield from parents(i, False)

    elif isinstance(o, dict):
        if not is_root and signed(o):
            yield o
        else:
            for i in o.values():
                yield from parents(i, False)


def sign_date(o):
    return datetime.datetime.utcfromtimestamp(o['_sig']['dat'])


# Returns the root user of an order
def user(o):
    return ({
        ALIAS_ACCESS: lambda o: user(o['grant']),
        ALIAS_AUTHZ: root_signer,
        ALIAS_BIND: root_signer,
        ALIAS_CERT: lambda o: None,
        ALIAS_REFRESH: lambda o: user(o['grant']),
        ALIAS_REGISTER: lambda o: None,
        ALIAS_REVOKE: lambda o: None,
        ALIAS_SUBKEY: lambda o: None,
    })[o['type']](o)


# Returns the root client of an order
def client(o):
    return ({
        ALIAS_ACCESS: lambda o: client(o['grant']),
        ALIAS_AUTHZ: lambda o: client(o['client']),
        ALIAS_BIND: lambda o: None,
        ALIAS_CERT: lambda o: None,
        ALIAS_REFRESH: lambda o: client(o['grant']),
        ALIAS_REGISTER: root_signer,
        ALIAS_REVOKE: lambda o: None,
        ALIAS_SUBKEY: lambda o: None,
    })[o['type']](o)
