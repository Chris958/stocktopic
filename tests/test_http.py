import ssl

from stocktopic.http import TLS_CONTEXT


def test_packaged_ca_context_keeps_certificate_verification_enabled():
    assert TLS_CONTEXT.check_hostname is True
    assert TLS_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    assert len(TLS_CONTEXT.get_ca_certs()) > 50
