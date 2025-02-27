import logging
from datetime import datetime, timedelta

import pytz
import requests
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from django_countries.fields import CountryField
from jsonfield import JSONField
from requests.auth import HTTPBasicAuth
from websecmap.organizations.models import Url
from websecmap.reporting.models import SeriesOfUrlsReportMixin
from websecmap.scanners.models import InternetNLV2Scan

log = logging.getLogger(__package__)

CREDENTIAL_CHECK_URL = "https://batch.internet.nl/api/"


class Account(models.Model):
    """
    An account is the entity that start scans. Multiple people can manage the account.
    """

    name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text=""
    )

    enable_scans = models.BooleanField(
        default=True,
    )

    internet_nl_api_username = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Internet.nl API Username"
    )

    # BinaryFields become MemoryView objects in postgres, which handle differently than normal strings.
    # For one, it's impossible to serialize them, which makes using Account objects impossible. Therefore
    # bytes are stored as a string, while ugly, it might just function better and more consistent.
    # https://code.djangoproject.com/ticket/27813
    internet_nl_api_password = models.TextField(
        blank=True,
        null=True,
        help_text="New values will automatically be encrypted.",
        editable=True
    )

    can_connect_to_internet_nl_api = models.BooleanField(
        default=False
    )

    report_settings = JSONField(
        help_text="This stores reporting preferences: what fields are shown in the UI and so on (if any other)."
                  "This field can be edited on the report page.",
        null=True,
        blank=True
    )

    """
    These password methods allow you to interact with encryption as if it were just storing and retrieving strings.
    See test_password_storage for example usage.
    """
    @staticmethod
    def encrypt_password(password):
        f = Fernet(settings.FIELD_ENCRYPTION_KEY)
        return str(f.encrypt(password.encode()))

    @staticmethod
    def connect_to_internet_nl_api(username: str, password: str):
        # This makes a connection to the internet.nl dashboard using .htaccess authentication.
        try:
            response = requests.get(
                CREDENTIAL_CHECK_URL,
                auth=HTTPBasicAuth(username, password),
                # a massive timeout for a large file.
                timeout=(5, 5)
            )

            # Any status code means the account is not valid.
            if response.status_code != 200:
                return False

            return True
        except requests.exceptions.ConnectionError:
            return False

    def decrypt_password(self):

        if not self.internet_nl_api_password:
            raise ValueError('Password was not set.')

        f = Fernet(settings.FIELD_ENCRYPTION_KEY)
        # Convert the string back to bytes again is not beautiful. But it's a bit more reliable than
        # storing the encrypted password in 'bytes', which somewhere goes wrong.
        return f.decrypt(bytes(self.internet_nl_api_password[2:-1], encoding='UTF-8')).decode('utf-8')

    def __str__(self):
        return "%s" % self.name


class DashboardUser(models.Model):
    """
    This connects auth.User to a user that is used in the Dashboard. It is common practice to extend auth.Model with a
    one to one relation with an extended model.
    An additional benefit/feature is that we can easily switch what user is connected to what account.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE
    )

    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
    )

    mail_preferred_mail_address = models.EmailField(
        help_text="This address can deviate from the account mail address for password resets and other account"
                  " features.",
        null=True,
        blank=True
    )
    mail_preferred_language = CountryField(default='EN')

    mail_send_mail_after_scan_finished = models.BooleanField(
        default=False,
        help_text="After a scan is finished, an e-mail is sent informing the user that a report is ready."
    )

    mail_after_mail_unsubscribe_code = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="This is autofilled when sending an e-mail. The user can use this code to set "
                  "mail_send_mail_after_scan_finished to false without logging in."
    )

    notes = models.TextField(
        max_length=800,
        blank=True,
        null=True,
    )

    def __str__(self):
        return "%s/%s" % (self.account, self.user)


class UrlList(models.Model):
    """
    This is a list of urls that will be scanned on internet.nl.
    This list will be extended with things like: 'what should be scanned (mail / web)'. What to show on the dashboard.
    """

    name = models.CharField(
        max_length=120,
        help_text="Name of the UrlList, for example name of the organization in it."
    )

    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        help_text="Who owns and manages this urllist."
    )

    urls = models.ManyToManyField(
        Url,
        blank=True,
        related_name='urls_in_dashboard_list'
    )

    enable_scans = models.BooleanField(
        default=True,
    )

    # todo: should we support both? How does that impact reporting?
    scan_type = models.CharField(
        max_length=4,
        choices=(
            ('web', 'web'),
            ('mail', 'mail'),
        ),
        default='web',
    )

    automated_scan_frequency = models.CharField(
        max_length=30,
        choices=(
            ('disabled', 'disabled'),
            ('every half year', 'every half year'),
            ('at the start of every quarter', 'at the start of every quarter'),
            ('every 1st day of the month', 'every 1st day of the month'),
            ('twice per month', 'twice per month'),
        ),
        default='disabled',
        help_text="At what moment should the scan start?"
    )

    scheduled_next_scan = models.DateTimeField(
        help_text="An indication at what moment the scan will be started. The scan can take a while, thus this does "
                  "not tell you when a scan will be finished. All dates in the past will be scanned and updated.",
        default=datetime(2030, 1, 1, 1, 1, 1, 601526, tzinfo=pytz.utc)
    )

    is_deleted = models.BooleanField(
        default=False,
    )

    deleted_on = models.DateTimeField(
        null=True,
        blank=True
    )

    last_manual_scan = models.DateTimeField(
        null=True,
        blank=True
    )

    def __str__(self):
        return "%s/%s" % (self.account, self.name)

    def is_due_for_scanning(self) -> bool:
        # when disabled, will not be scanned automatically anymore.
        if self.automated_scan_frequency == 'disabled':
            return False

        return timezone.now() > self.scheduled_next_scan

    def renew_scan_moment(self) -> None:
        self.scheduled_next_scan = self.determine_next_scan_moment(self.automated_scan_frequency)
        self.save(update_fields=['scheduled_next_scan'])

    # todo: write test
    @staticmethod
    def determine_next_scan_moment(preference: str):
        """
        Converts one of the (many) string options to the next sensible date/time combination in the future.

        disabled: yesterday.
        every half year: first upcoming 1 july or 1 january
        at the start of every quarter: 1 january, 1 april, 1 juli, 1 october
        every 1st day of the month: 1 january, 1 february, etc.
        twice per month: 1 january, 1 january + 2 weeks, 1 february, 1 february + 2 weeks, etc

        :param preference:
        :return:
        """
        now = timezone.now()

        if preference == 'disabled':
            # far, far in the future, so it will not be scanned and probably will be re-calculated. 24 years...
            return now + timedelta(days=9000)

        # months are base 1: january = 1 etc.
        if preference == 'every half year':
            if now.month in [1, 2, 3, 4, 5, 6]:
                return datetime(year=now.year, month=7, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)
            return datetime(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)

        if preference == 'at the start of every quarter':

            if now.month in [1, 2, 3]:
                return datetime(year=now.year, month=4, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)
            if now.month in [4, 5, 6]:
                return datetime(year=now.year, month=7, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)
            if now.month in [7, 8, 9]:
                return datetime(year=now.year, month=10, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)
            if now.month in [10, 11, 12]:
                return datetime(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)

        if preference == 'every 1st day of the month':
            if now.month == 12:
                return datetime(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)
            return datetime(year=now.year, month=now.month + 1, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)

        if preference == 'twice per month':
            # since the 14'th day never causes a month or year rollover, we can simply schedule for the 15th day.
            # note: range is not used because range is _to_ a certain moment.
            if now.day in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]:
                return datetime(year=now.year, month=now.month, day=15, hour=0, minute=0, second=0, tzinfo=pytz.utc)

            # otherwise exactly the same as the 1st day of every month
            if now.month == 12:
                return datetime(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)
            return datetime(year=now.year, month=now.month + 1, day=1, hour=0, minute=0, second=0, tzinfo=pytz.utc)

        raise ValueError('String %s could not be translated to a scan moment.' % preference)

    # @pysnooper.snoop()
    def is_scan_now_available(self) -> bool:
        """
        Requirements for availability:

        - The last scan for this list has to be finished.
        - Deprecated: when a scan is finished, you can scan again -> The scan was not run earlier in the past N hours.
        - Scanning for this list has been enabled.

        :param self:
        :return:
        """

        if not self.enable_scans:
            # log.debug("Scan now NOT available: List %s has disabled scans." % self)
            return False

        # Deprecated: At least N hours should have passed since the last manual scan.
        # yesterday = timezone.now() - timedelta(hours=1)
        # manual scans have their own 'is available flag', as the 'create_dashboard_scan_tasks()' does not guarantee
        # a new scan is created instantly. The flag needs to be set otherwise a user can initiate tons of scans.
        # if self.last_manual_scan and self.last_manual_scan > yesterday:
            # log.debug("Scan now NOT available: Last manual scan was in the last 24 hours for list %s" % self)
        #   return False
        # End deprecation

        last_scan = AccountInternetNLScan.objects.all().filter(urllist=self, urllist__is_deleted=False).last()

        if not last_scan:
            # log.debug("Scan now available: a previous scan was never performed on list %s" % self)
            return True

        # no get method on scan object... 30 is an arbitrary number that is long enough to again allow scans.
        # finished_on = last_scan.scan.finished_on \
        #     if last_scan.scan.finished_on else timezone.now() - timedelta(days=30)
        # and finished_on < yesterday
        if last_scan.state in ['finished', 'cancelled']:
            # log.debug("Scan now available: last scan finished over 24 hours ago on list %s" % self)
            return True

        # log.debug("Scan now NOT available: Last scan might be in the last 24 hours. %s" % self)
        return False


class UploadLog(models.Model):
    original_filename = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="The original filename of the file that has been uploaded. Django appends a random string if the "
                  "file already exists. This is a reconstruction of the original filename and may not be 100% accurate."
    )

    internal_filename = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Generated filename by Django. This can be used to find specific files for debugging purposes."
    )

    status = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="If the upload was successful or not. Might contain 'success' or 'error'."
    )

    message = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="This message gives more specific information about what happened. For example, it might be the "
                  "case that a file has been rejected because it had the wrong filetype etc."
    )

    upload_date = models.DateTimeField(
        blank=True,
        null=True
    )

    filesize = models.PositiveIntegerField(
        default=0,
        blank=False,
        null=False,
        help_text="Gives an indication if your local file has changed (different size). The size is in bytes."
    )

    user = models.ForeignKey(
        DashboardUser,
        on_delete=models.CASCADE,
        help_text="What user performed this upload.",
        blank=True,
        null=True
    )


class UrlListReport(SeriesOfUrlsReportMixin):
    """
    This is basically an aggregation of UrlRating

    Contains aggregated ratings over time. Why?

    - Reduces complexity to get ratings
        You don't need to know about dead(urls, endpoints), scanner-results.
        For convenience purposes a calculation field also contains some hints why the rating is
        the way it is.

    -   It increases speed
        Instead of continuously calculating the score, it is done on a more regular interval: for
        example once every 10 minutes and only for the last 10 minutes.

    A time dimension is kept, since it's important to see what the rating was over time. This is
    now very simple to get (you don't need a complex join which is hard in django).

    The client software does a drill down on domains and shows why things are the way they are.
    Also this should not know too much about different scanners. In OO fashion, it should ask a
    scanner to explain why something is the way it is (over time).
    """
    urllist = models.ForeignKey(UrlList, on_delete=models.CASCADE)

    average_internet_nl_score = models.FloatField(
        help_text="Internet.nl scores are retrieved in point. The calculation done for that is complex and "
                  "subject to change over time. Therefore it is impossible to re-calculate that score here."
                  "Instead the score is stored as a given.",
        default=0,
    )

    class Meta:
        get_latest_by = "at_when"
        index_together = [
            ["at_when", "id"],
        ]

    def get_previous_report_from_this_list(self):
        """
        Get's the previous report of this list. This is useful for creating comparisons.
        If there is no previous report None is returned. Calculation is not included by default because it
        makes sorting very slow. The deferred field is retrieved when requested explicitly with a direct query.
        :return:
        """
        try:
            return UrlListReport.objects.all().filter(
                urllist=self.urllist,
                at_when__lt=self.at_when
            ).exclude(id=self.id).defer('calculation').latest()
        except UrlListReport.DoesNotExist:
            return None


class AccountInternetNLScan(models.Model):
    """
    We've explicitly not declared the N-to-N on scan or account. This is the N to N between them. When a new scan is
    created the UrlList serves as extra data.
    """

    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
    )

    scan = models.ForeignKey(
        InternetNLV2Scan,
        on_delete=models.CASCADE,

        # When there is no scan registered at internet.nl, but the scan has to show up as requested
        null=True
    )

    urllist = models.ForeignKey(
        UrlList,
        on_delete=models.CASCADE,
    )

    # The current state of the scan.
    state = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The current state"
    )

    started_on = models.DateTimeField(
        blank=True,
        null=True
    )

    finished_on = models.DateTimeField(
        blank=True,
        null=True
    )

    state_changed_on = models.DateTimeField(
        blank=True,
        null=True
    )

    report = models.ForeignKey(
        UrlListReport,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="After a scan has finished, a report is created. This points to that report so no guessing "
                  "is needed to figure out what report belongs to what scan."
    )

    @property
    def finished(self):
        return self.state == "finished"


class AccountInternetNLScanLog(models.Model):
    scan = models.ForeignKey(
        AccountInternetNLScan,
        on_delete=models.CASCADE,
    )

    state = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The state that was registered at a certain moment in time."
    )

    at_when = models.DateTimeField(
        blank=True,
        null=True
    )
