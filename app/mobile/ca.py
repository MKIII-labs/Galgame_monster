# -*- coding: utf-8 -*-
"""本地 CA 与局域网 HTTPS 证书（手机装一次根证书即可无警告访问）。

根证书 10 年；站点证书 1 年，SAN 覆盖本机局域网 IP，IP 变化时自动重签。
"""
import datetime
import hashlib
import ipaddress
import os
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.mobile import TLS_DIR

_LOCK = threading.RLock()
CA_CERT = os.path.join(TLS_DIR, "ca.pem")
CA_KEY = os.path.join(TLS_DIR, "ca.key")
SRV_CERT = os.path.join(TLS_DIR, "server.pem")
SRV_KEY = os.path.join(TLS_DIR, "server.key")
CA_NAME = "Galgame Monster Mobile CA"


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _load_cert(path):
    try:
        with open(path, "rb") as f:
            return x509.load_pem_x509_certificate(f.read())
    except (OSError, ValueError):
        return None


def _load_key(path):
    try:
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except (OSError, ValueError, TypeError):
        return None


def _key_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def _cert_bytes(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def ensure_ca():
    """返回 (ca_cert, ca_key)；不存在则生成。"""
    with _LOCK:
        os.makedirs(TLS_DIR, exist_ok=True)
        cert, key = _load_cert(CA_CERT), _load_key(CA_KEY)
        if cert is not None and key is not None:
            return cert, key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = _name(CA_NAME)
        cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                               critical=False)
                .sign(key, hashes.SHA256()))
        _write(CA_KEY, _key_bytes(key))
        _write(CA_CERT, _cert_bytes(cert))
        return cert, key


def _san_values(ips):
    values = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for raw in ips or []:
        try:
            addr = ipaddress.ip_address(str(raw))
        except ValueError:
            continue
        if addr not in [v.value for v in values if isinstance(v, x509.IPAddress)]:
            values.append(x509.IPAddress(addr))
    return values


def _cert_covers(cert, values):
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = set(ext.get_values_for_type(x509.IPAddress))
        want = set(v.value for v in values if isinstance(v, x509.IPAddress))
        return want.issubset(have)
    except x509.ExtensionNotFound:
        return False


def ensure_server_cert(ips):
    """确保站点证书覆盖给定 IP；返回 (cert_path, key_path)。"""
    with _LOCK:
        ca_cert, ca_key = ensure_ca()
        values = _san_values(ips)
        cert, key = _load_cert(SRV_CERT), _load_key(SRV_KEY)
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh = False
        if cert is not None and key is not None:
            try:
                fresh = (cert.not_valid_after_utc > now + datetime.timedelta(days=7)
                         and cert.issuer == ca_cert.subject
                         and _cert_covers(cert, values))
            except (AttributeError, ValueError):
                fresh = False
        if fresh:
            return SRV_CERT, SRV_KEY
        key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = _name("Galgame Monster")
        cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(ca_cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=397))
                .add_extension(x509.SubjectAlternativeName(values), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                               critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                               critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        _write(SRV_KEY, _key_bytes(key))
        _write(SRV_CERT, _cert_bytes(cert))
        return SRV_CERT, SRV_KEY


def ca_fingerprint():
    """根证书指纹（SHA-256，冒号分隔大写十六进制），用于手机安装时核对。"""
    cert, _key = ensure_ca()
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def existing_fingerprint():
    """已生成过根证书时返回指纹；没有就返回空串（不触发证书生成）。"""
    cert = _load_cert(CA_CERT)
    if cert is None:
        return ""
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def ca_pem():
    cert, _key = ensure_ca()
    return _cert_bytes(cert)


def ssl_context(ips):
    cert_path, key_path = ensure_server_cert(ips)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    try:
        ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!RC4")
    except ssl.SSLError:
        pass
    return ctx
