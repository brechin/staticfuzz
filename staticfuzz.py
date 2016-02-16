"""staticfuzz: memories that vanish.

Async server-sent event (SSE) text+board.

If this script is invoked directly, it can be used
as a CLI for managing staticfuzz.

You can test this by running:
    gunicorn -b 127.0.0.1:5000 -k gevent main:app

Usage:
    staticfuzz.py init_db
    staticfuzz.py serve
    staticfuzz.py -h | --help

Options:
    -h --help    Show this screen.

"""

import mimetypes
import os
import random
import re
import urllib

import markupsafe
import requests
from google.appengine.ext import ndb

import flask
import glitch
from flask_limiter import Limiter

# Create and init the staticfuzz
app = flask.Flask(__name__)
app.config.from_object("config")
limiter = Limiter(app)


class Memory(ndb.Model):
    """
    Memories are aptly kept in the "memories" table.

    Fields/attributes:
        id (int): Unique identifier, never resets.
        text (str): String, the text of the memory, the
            memory itself.
        base64_image str): if `text` is a URI to an image,
            then this is the base64 encoding of said
            image. Used as thumbnail.

    """
    timestamp = ndb.DateTimeProperty(auto_now_add=True)
    text = ndb.StringProperty(required=True)
    base64_image = ndb.TextProperty(default=None)

    @classmethod
    def create(cls, text):
        """Create a new memory with text and optionally base64
        representation of the image content found at the URI in
        the text argument.

        Args:
            text (str): This is required for all memories. If this
                is a link to an image, a base64_image thumbnail will
                be generated for said image.

        """
        created_memory = Memory(text=text)
        if uri_valid_image(text):  # valid uri to image?
            created_memory.base64_image = glitch.glitch_from_url(text)
        return created_memory

    def to_dict(self):
        """Return a dictionary representation of this Memory.

        This is for sending as an event.

        Returns:
            dict: Looks something like this:

                >>> {'text': "foo", "base64_image": None}

        """

        timestamp = self.timestamp.isoformat("T") + 'Z'

        return {"text": self.text,
                "timestamp": timestamp,
                "base64_image": self.base64_image,
                "id": self.key.id()}


class SlashCommandResponse(object):
    """All SlashCommand.callback() methods must return this.

    Attributes:
        create_memory (bool): If True create a memory using
            value attribute. Otherwise serve value as a
            response.
        value (tuple[str, int]|str): Either a tuple which
            holds a status message and an HTTP error code,
            or a string to create a memory. Example values:

                >>> "some memory text"
                >>> ("Bad ID", 400)

    See Also:
        SlashCommand

    """

    def __init__(self, create_memory, value):
        """The result of a slash command.

        Args:
            create_memory (bool): If True the value will be
                used to create a memory. If False value will be
                sent as a response, e.g., "Invalid URI," 400.
            value (any): A string to be used for creating a
                memory, or a response to be sent like:

                >>> ("Invalid Shipping Address", 400)

        Examples:

            >>> SlashCommandResponse(False, ("Bad thing!", 400))
            >>> SlashCommandResponse(True, "some kinda memory text")

        """

        self.create_memory = create_memory
        self.value = value


class SlashCommand(object):
    """IRC-like command; a more complicated memory.

    When input text is received, it is also sent to each
    slash command's attempt() method. The first attempt
    to return a SlashCommandResponse gets used either to
    create a memory or serve a response (like a 400).

    """

    NAME = str()

    @classmethod
    def attempt(cls, text):
        """Return either None or the SlashCommandResponse
        associated with running the command.

        Returns:
            SlashCommandResponse|None: Returns None if
                `text` is not even this command. Otherwise
                return the result of executing this command.

        """

        text = text.lower().strip()
        pattern = '/' + cls.NAME

        if not re.match(r'%s\s*' % pattern, text):
            return None

        text = text.replace(pattern, '')
        args = [arg.strip() for arg in text.split(' ') if arg.strip()]

        try:
            return cls.callback(*args)

        # NOTE: what if typeerror is raised because of something
        # in the callback. Seems like a bad approach... could
        # cause debugging headaches.
        except:
            raise
            # except TypeError as ex:
            #     return SlashCommandResponse(False, ('%s incorrect args' %
            #                                         cls.NAME, 400))

    @staticmethod
    def callback(*args):
        """Override this with another staticmethod; do something
        with the args, return a SlashCommandResponse.

        Use any number of args you please (including 0) or *args
        (variable length).

        """

        pass


class SlashLogin(SlashCommand):
    """Login as deity if the secret is correct.

    Note:
        You have to refresh the page for this to work.

    See Also:
        SlashLogout

    """

    NAME = 'login'

    @staticmethod
    def callback(secret_attempt):
        """Attempt to use secret_attempt to identify as a deity.

        Args:
            secret_attempt (str): Password/whisper secret
                being attempted. If the secret corresponds
                to the one set in the config file, the
                session's "deity" key is set to True.

        Returns:
            SlashCommandResponse: Either a login failure 401, or
                redirect to the show_memories page after success.

        """

        if secret_attempt == app.config['WHISPER_SECRET']:
            flask.session['deity'] = True
            flask.flash(app.config["DEITY_GREET"])
            redirect = flask.redirect(flask.url_for('show_memories'))

            return SlashCommandResponse(False, redirect)

        else:

            return SlashCommandResponse(False, (app.config["LOGIN_FAIL"], 401))


class SlashLogout(SlashCommand):
    """Stop being a deity.

    Note:
        You have to refresh the page for this to work.

    """

    NAME = 'logout'

    @staticmethod
    def callback():
        """User who sent this will no longer be a deity.

        Returns:
            SlashCommandResponse: redirect to show_memories.

        """

        flask.session.pop('deity', None)
        flask.flash(app.config["DEITY_GOODBYE"])
        redirect = flask.redirect(flask.url_for('show_memories'))

        return SlashCommandResponse(False, redirect)


class SlashDanbooru(SlashCommand):
    """Get a random image from the first page of a search
    for specific tags on danbooru.donmai.us.

    See Also:
        http://danbooru.donmai.us/wiki_pages/43568 

    """

    NAME = 'danbooru'

    @staticmethod
    def callback(*args):
        """Each arg is a Danbooru tag.

        Args:
          *args (list[str]): Each element is a string/tag
            to search for, e.g., "goo_girl"

        Returns:
            SlashCommandResponse: Either the random image found,
                or a 400 for whatever error encountered while
                attempting to get a random image.

        """

        tags = urllib.quote_plus(' '.join(args))
        endpoint = ('http://danbooru.donmai.us/posts.json?'
                    'tags=%s&limit=10&page1' % tags)
        resp = requests.get(endpoint)
        results = resp.json()
        try:
            selected_image = ("http://danbooru.donmai.us" +
                              random.choice(results)["file_url"])

            return SlashCommandResponse(True, selected_image)

        except IndexError:

            # There were no results!
            return SlashCommandResponse(False,
                                        (app.config["ERROR_DANBOORU"], 400))


@app.template_filter('number_links')
def number_links(string_being_filtered):
    escaped_string = markupsafe.escape(string_being_filtered)
    unicode_escaped_string = unicode(escaped_string)
    pattern = re.compile(r'(?<!&)(#\d+)')
    final_string = pattern.subn(r'<a href="\1">\1</a>',
                                unicode_escaped_string)[0]

    return final_string


def uri_valid_image(uri):
    """Check if provided `uri` is a valid URI to
    an image, as allowed by the extension whitelist.

    Returns:
        bool: True if `uri` is both an accessible
            URI without any errors *and* the file
            extension is in the whitelist.

    """

    image_extension_whitelist = (".jpg", ".jpeg", ".png", ".gif")
    if not uri.lower().endswith(image_extension_whitelist):
        return False

    # actually fetch the resource to see if it's real or not
    try:
        assert uri.startswith('http')
        resp = requests.head(uri)
        assert resp.status_code == 200
    except (requests.exceptions.InvalidSchema,
            requests.exceptions.MissingSchema,
            requests.exceptions.ConnectionError,
            AssertionError):
        return False

    return True


@app.errorhandler(429)
def ratelimit_handler(error):
    """Handle rate exceeding error message.

    Args:
        error (?): automagically provided

    """
    return app.config["ERROR_RATE_EXCEEDED"], 429


@app.route('/random_image', methods=['GET', 'POST'])
@limiter.limit("2/second")
def random_image():
    image_directory = app.config["RANDOM_IMAGE_DIRECTORY"]
    images = os.listdir(image_directory)
    image_path = os.path.join(
        image_directory,
        random.choice(images)
    )
    return flask.send_file(image_path, mimetypes.guess_type(image_path)[0])


# def event():
#     """EventSource stream; server side events. Used for
#     sending out new memories.
#
#     Returns:
#         json event (str): --
#
#     See Also:
#         stream()
#
#     """
#     with app.app_context():
#
#         try:
#             latest_memory_id = Memory.query().order(Memory.timestamp).get(keys_only=True).id()
#         except AttributeError:
#             # .id will raise AttributeError if the query doesn't match anything
#             latest_memory_id = 0
#
#     while True:
#
#         with app.app_context():
#             memories = (Memory.query.filter(Memory.id > latest_memory_id).
#                         order_by(Memory.id.asc()).all())
#
#         if memories:
#             latest_memory_id = memories[-1].id
#             newer_memories = [memory.to_dict() for memory in memories]
#
#             yield "data: " + json.dumps(newer_memories) + "\n\n"
#
#         with app.app_context():
#             gevent.sleep(app.config['SLEEP_RATE'])


# @app.route('/stream/', methods=['GET', 'POST'])
# @limiter.limit("15/minute")
# def stream():
#     """SSE (Server Side Events), for an EventSource. Send
#     the event of a new message.
#
#     See Also:
#         event()
#
#     """
#
#     return flask.Response(event(), mimetype="text/event-stream")


@app.route('/')
@limiter.limit("2/second")
def show_memories():
    """Show the memories.

    """
    memories = Memory.query().order(Memory.timestamp)
    memories_for_jinja = [memory.to_dict() for memory in memories]
    return flask.render_template('show_memories.html',
                                 memories=memories_for_jinja)


def validate(memory_text):
    """Return None if validation successful, else
    return 400 + status message.

    """

    # memory must be at least 1 char
    if len(memory_text) == 0:
        return 'Too short!', 400

    # memory text may not exceed MAX_CHARACTERS
    if len(memory_text) > app.config['MAX_CHARACTERS']:
        return app.config["ERROR_TOO_LONG"], 400

    # you cannot repost something already in the memories
    if Memory.query(Memory.text == memory_text).count() > 0:
        return app.config["ERROR_UNORIGINAL"], 400

    # success!
    return None


@app.route('/new_memory', methods=['POST'])
@limiter.limit("1/second")
def new_memory():
    """Attempt to add a new memory.

    Forget the 11th oldest memory.

    The memory must meet these requirements:

      * At least 1 character long
      * 140 characters or less
      * Cannot already exist in the database

    The memory is checked for possible SlashCommand(s).

    """

    memory_text = flask.request.form['text'].strip()
    original_memory_text = memory_text
    validation_payload = validate(memory_text)

    if validation_payload:
        return validation_payload

    # commands
    slash_commands = [SlashLogin, SlashLogout, SlashDanbooru]

    for slash_command in slash_commands:
        result = slash_command.attempt(memory_text)
        if result is None:
            # only happens if it is a non-match
            continue

        elif result.create_memory is True:
            memory_text = result.value
            break
        elif result.create_memory is False:
            return result.value

    # We do not want to submit anything that didn't execute
    # a slash command, but starts with a slash! This is in
    # case of an event like "/login password", so it's not
    # broadcasted to the entire world.
    #
    # At this point, either we have made a change to the
    # database and return'd out, or we modified the memory
    # text, in which it'll differ from original_memory.
    if memory_text[0] == '/' and memory_text == original_memory_text:
        return "Invalid Slash Command", 400

    # If there are ten memories already, delete the oldest
    # to make room!
    while Memory.query().count() == 10:
        memory_to_delete = Memory.query().order(Memory.timestamp).get(keys_only=True)
        memory_to_delete.delete()
    new_memory = Memory.create(memory_text)
    new_memory.put()

    return flask.redirect(flask.url_for('show_memories'))


@app.route('/forget', methods=['POST'])
def forget():
    """Deities can make us all forget.

    Delete a memory.

    Returns:
        flask redirect to show_memories or send a 401
            unauthorized HTTP error.

    """

    if not flask.session.get('deity'):
        flask.abort(401)

    memory_key_to_forget = ndb.Key('Memory', int(flask.request.form["id"]))
    memory_key_to_forget.delete()

    return flask.redirect(flask.url_for('show_memories'))
