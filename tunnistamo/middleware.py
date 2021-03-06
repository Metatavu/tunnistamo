import json
import logging
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.utils import timezone
from django.utils.http import quote
from oidc_provider.lib.errors import BearerTokenError
from social_django.middleware import SocialAuthExceptionMiddleware

logger = logging.getLogger(__name__)


class InterruptedSocialAuthMiddleware(SocialAuthExceptionMiddleware):
    # Override get_redirect_uri to point the user back to backend selection
    # instead of non-functioning login page in case of authentication error
    def get_redirect_uri(self, request, exception):
        strategy = getattr(request, 'social_strategy', None)
        if strategy.session.get('next') is None:
            return super().get_redirect_uri(request, exception)
        url = '/login/?next=' + quote(strategy.session.get('next'))
        return url

    # Override raise_exception() to allow redirect also when debug is enabled
    def raise_exception(self, request, exception):
        strategy = getattr(request, 'social_strategy', None)
        if strategy is not None:
            return strategy.setting('RAISE_EXCEPTIONS')


class OIDCExceptionMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, BearerTokenError):
            response = HttpResponseForbidden()
            auth_fields = [
                'error="{}"'.format(exception.code),
                'error_description="{}"'.format(exception.description)
            ]
            if 'scope' in request.POST:
                auth_fields = ['Bearer realm="{}"'.format(request.POST['scope'])] + auth_fields
            response.__setitem__('WWW-Authenticate', ', '.join(auth_fields))
            return response


class RestrictedAuthenticationMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, 'user', None) and request.user.is_authenticated and \
           request.session.get('_auth_user_backend') in settings.RESTRICTED_AUTHENTICATION_BACKENDS:
            if request.user.last_login + timedelta(seconds=settings.RESTRICTED_AUTHENTICATION_TIMEOUT) < timezone.now():
                logger.info('Restricted session has timed out. Session started at {}'.format(request.user.last_login))
                response = HttpResponseRedirect(request.get_full_path())
                request.session.delete()
                return response
        return self.get_response(request)


class ContentSecurityPolicyMiddleware(object):
    HEADER_ENFORCING = 'Content-Security-Policy'
    HEADER_REPORTING = 'Content-Security-Policy-Report-Only'

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def get_csp_settings(settings):
        return getattr(settings, 'CONTENT_SECURITY_POLICY', None)

    @staticmethod
    def find_policy(csp_settings):
        return csp_settings is not None and csp_settings.get('policy') is not None

    def __call__(self, request):
        response = self.get_response(request)
        csp_settings = ContentSecurityPolicyMiddleware.get_csp_settings(settings)
        if not ContentSecurityPolicyMiddleware.find_policy(csp_settings):
            return response

        if csp_settings.get('report_only') is True:
            header = self.HEADER_REPORTING
        else:
            header = self.HEADER_ENFORCING
        response[header] = csp_settings['policy']

        if csp_settings.get('report_groups') and len(csp_settings.get('report_groups', {})) > 0:
            response['Report-To'] = json.dumps(csp_settings['report_groups'])
        return response
