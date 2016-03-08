from __future__ import unicode_literals

import os
import unittest
from datetime import datetime, timedelta
from time import mktime, sleep

import requests
from django.test import SimpleTestCase
from django.test.utils import override_settings

from anymail.exceptions import AnymailAPIError
from anymail.message import AnymailMessage

from .utils import sample_image_content, AnymailTestMixin


MAILGUN_TEST_API_KEY = os.getenv('MAILGUN_TEST_API_KEY')
MAILGUN_TEST_DOMAIN = os.getenv('MAILGUN_TEST_DOMAIN')


@unittest.skipUnless(MAILGUN_TEST_API_KEY and MAILGUN_TEST_DOMAIN,
                     "Set MAILGUN_TEST_API_KEY and MAILGUN_TEST_DOMAIN environment variables "
                     "to run Mailgun integration tests")
@override_settings(ANYMAIL_MAILGUN_API_KEY=MAILGUN_TEST_API_KEY,
                   ANYMAIL_MAILGUN_SEND_DEFAULTS={
                       'esp_extra': {'o:testmode': 'yes',
                                     'sender_domain': MAILGUN_TEST_DOMAIN}},
                   EMAIL_BACKEND="anymail.backends.mailgun.MailgunBackend")
class MailgunBackendIntegrationTests(SimpleTestCase, AnymailTestMixin):
    """Mailgun API integration tests

    These tests run against the **live** Mailgun API, using the
    environment variable `MAILGUN_TEST_API_KEY` as the API key
    and `MAILGUN_TEST_DOMAIN` as the sender domain.
    If those variables are not set, these tests won't run.

    """

    def setUp(self):
        super(MailgunBackendIntegrationTests, self).setUp()
        self.message = AnymailMessage('Anymail integration test', 'Text content',
                                      'from@example.com', ['to@example.com'])
        self.message.attach_alternative('<p>HTML content</p>', "text/html")

    def fetch_mailgun_events(self, message_id, event=None,
                             initial_delay=2, retry_delay=1, max_retries=5):
        """Return list of Mailgun events related to message_id"""
        url = "https://api.mailgun.net/v3/%s/events" % MAILGUN_TEST_DOMAIN
        auth = ("api", MAILGUN_TEST_API_KEY)

        # Despite the docs, Mailgun's events API actually expects the message-id
        # without the <...> brackets (so, not exactly "as returned by the messages API")
        # https://documentation.mailgun.com/api-events.html#filter-field
        params = {'message-id': message_id[1:-1]}  # strip <...>
        if event is not None:
            params['event'] = event

        # It can take a few seconds for the events to show up
        # in Mailgun's logs, so retry a few times if necessary:
        sleep(initial_delay)
        for retry in range(max_retries):
            if retry > 0:
                sleep(retry_delay)
            response = requests.get(url, auth=auth, params=params)
            response.raise_for_status()
            items = response.json()["items"]
            if len(items) > 0:
                return items
        return None

    def test_simple_send(self):
        # Example of getting the Mailgun send status and message id from the message
        sent_count = self.message.send()
        self.assertEqual(sent_count, 1)

        anymail_status = self.message.anymail_status
        sent_status = anymail_status.recipients['to@example.com'].status
        message_id = anymail_status.recipients['to@example.com'].message_id

        self.assertEqual(sent_status, 'queued')  # Mailgun always queues
        self.assertGreater(len(message_id), 0)  # don't know what it'll be, but it should exist

        self.assertEqual(anymail_status.status, {sent_status})  # set of all recipient statuses
        self.assertEqual(anymail_status.message_id, message_id)

    def test_all_options(self):
        send_at = datetime.now().replace(microsecond=0) + timedelta(minutes=2)
        send_at_timestamp = mktime(send_at.timetuple())  # python3: send_at.timestamp()
        message = AnymailMessage(
            subject="Anymail all-options integration test",
            body="This is the text body",
            from_email="Test From <from@example.com>",
            to=["to1@example.com", "Recipient 2 <to2@example.com>"],
            cc=["cc1@example.com", "Copy 2 <cc2@example.com>"],
            bcc=["bcc1@example.com", "Blind Copy 2 <bcc2@example.com>"],
            reply_to=["reply1@example.com", "Reply 2 <reply2@example.com>"],
            headers={"X-Anymail-Test": "value"},

            metadata={"meta1": "simple string", "meta2": 2, "meta3": {"complex": "value"}},
            send_at=send_at,
            tags=["tag 1", "tag 2"],
            track_clicks=False,
            track_opens=True,
        )
        message.attach("attachment1.txt", "Here is some\ntext for you", "text/plain")
        message.attach("attachment2.csv", "ID,Name\n1,3", "text/csv")
        cid = message.attach_inline_image(sample_image_content(), domain=MAILGUN_TEST_DOMAIN)
        message.attach_alternative(
            "<div>This is the <i>html</i> body <img src='cid:%s'></div>" % cid,
            "text/html")

        message.send()
        self.assertEqual(message.anymail_status.status, {'queued'})  # Mailgun always queues
        message_id = message.anymail_status.message_id

        events = self.fetch_mailgun_events(message_id, event="accepted")
        if events is None:
            self.skipTest("No Mailgun 'accepted' event after 30sec -- can't complete this test")
            return

        event = events.pop()
        self.assertCountEqual(event["tags"], ["tag 1", "tag 2"])  # don't care about order
        self.assertEqual(event["user-variables"],
                         {"meta1": "simple string",
                          "meta2": "2",  # numbers become strings
                          "meta3": '{"complex": "value"}'})  # complex values become json

        self.assertEqual(event["message"]["scheduled-for"], send_at_timestamp)
        self.assertCountEqual(event["message"]["recipients"],
                              ['to1@example.com', 'to2@example.com', 'cc1@example.com', 'cc2@example.com',
                               'bcc1@example.com', 'bcc2@example.com'])  # don't care about order

        headers = event["message"]["headers"]
        self.assertEqual(headers["from"], "Test From <from@example.com>")
        self.assertEqual(headers["to"], "to1@example.com, Recipient 2 <to2@example.com>")
        self.assertEqual(headers["subject"], "Anymail all-options integration test")

        attachments = event["message"]["attachments"]
        self.assertEqual(len(attachments), 2)  # because inline image shouldn't be an attachment
        self.assertEqual(attachments[0]["filename"], "attachment1.txt")
        self.assertEqual(attachments[0]["content-type"], "text/plain")
        self.assertEqual(attachments[1]["filename"], "attachment2.csv")
        self.assertEqual(attachments[1]["content-type"], "text/csv")

        # No other fields are verifiable from the event data.
        # (We could try fetching the message from event["storage"]["url"]
        # to verify content and other headers.)

    def test_invalid_from(self):
        self.message.from_email = 'webmaster'
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        err = cm.exception
        self.assertEqual(err.status_code, 400)
        self.assertIn("'from' parameter is not a valid address", str(err))

    @override_settings(ANYMAIL_MAILGUN_API_KEY="Hey, that's not an API key!")
    def test_invalid_api_key(self):
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        err = cm.exception
        self.assertEqual(err.status_code, 401)
        # Mailgun doesn't offer any additional explanation in its response body
        # self.assertIn("Forbidden", str(err))
