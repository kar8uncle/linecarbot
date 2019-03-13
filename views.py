from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

from linebot import LineBotApi, WebhookParser, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, StickerMessage, ImageMessage, VideoMessage, AudioMessage, FileMessage

import functools
import operator
import requests
import mimetypes
import logging
import json

logger = logging.getLogger(__name__)

class DiscordCarbot:
    repeat_hook_url = (
        'https://discordapp.com/api/webhooks/{repeat_webhook_id}/{repeat_webhook_token}'
        .format(**settings.DISCORD)
    )

    broadcast_hook_url = (
        'https://discordapp.com/api/webhooks/{broadcast_webhook_id}/{broadcast_webhook_token}'
        .format(**settings.DISCORD)
    )

    @staticmethod
    def send_message(hook_url, content=None, file=None, embeds=None, username=None, avatar_url=None, payload_json=None, tts=False):
        """ Sends a message to Discord. Passes thru Discord API.
        
            Refer to Discord API documentation.
        """
        data = locals()
        # file needs to be separately handled,
        # remove it from data dict
        del data['file']

        response = requests.post(hook_url, 
                                 json=data, # when there is embeds (line stickers), must use json
                                 data=data if file is not None else None, # when there is file, json can't be used, fallback to use form data
                                 files={ 'file' : file } if file is not None else None
                                 )

        try:
            response.raise_for_status()
        except:
            requests.post(hook_url,
                          data=dict(content='Unable to forward a message from Line.')
                          )
            logger.error('Unable to forward a message from Line. Locals: {}, response: {}'.format(str(locals()), str(response.text)))

class LineCarbot:
    handler = WebhookHandler(settings.LINE['secret'])
    api = LineBotApi(settings.LINE['token'])
    listening_groups = [ settings.LINE['capture_group_id'] ]

    @handler.add(MessageEvent, message=TextMessage)
    def handle_text_message(event):
        if hasattr(event.source, 'group_id'):
            if event.source.group_id in LineCarbot.listening_groups:
                DiscordCarbot.send_message(
                    DiscordCarbot.repeat_hook_url,
                    content=event.message.text,
                    **LineCarbot.get_user_overrides(event.source.user_id)
                )
        elif LineCarbot.user_in_listening_group(event.source.user_id):
            logger.info('Sent a private text message with content {}.'.format(event.message.text))
            DiscordCarbot.send_message(
                DiscordCarbot.broadcast_hook_url,
                content=event.message.text,
            )
        else:
            logger.info('Text message ignored as message source {} is not from listening groups.'.format(event.source))

    @handler.add(MessageEvent, message=StickerMessage)
    def handle_sticker_message(event):
        if hasattr(event.source, 'group_id'):
            if event.source.group_id in LineCarbot.listening_groups:
                DiscordCarbot.send_message(
                    DiscordCarbot.repeat_hook_url,
                    **LineCarbot.get_sticker_embed(event.message),
                    **LineCarbot.get_user_overrides(event.source.user_id)
                )
        elif LineCarbot.user_in_listening_group(event.source.user_id):
            logger.info('User {} sent a private sticker message, not forwarding since sending stickers through bot is not supported'.format(event.source))
        else:
            logger.info('Sticker message ignored as message source {} is not from listening groups.'.format(event.source))


    @handler.add(MessageEvent, message=ImageMessage)
    @handler.add(MessageEvent, message=VideoMessage)
    @handler.add(MessageEvent, message=AudioMessage)
    @handler.add(MessageEvent, message=FileMessage)
    def handle_file_message(event):
        if hasattr(event.source, 'group_id'):
            if event.source.group_id in LineCarbot.listening_groups:
                DiscordCarbot.send_message(
                    DiscordCarbot.repeat_hook_url,
                    **LineCarbot.get_file(event.message.id),
                    **LineCarbot.get_user_overrides(event.source.user_id)
                )
        elif LineCarbot.user_in_listening_group(event.source.user_id):
            logger.info('User {} sent a private file message.'.format(event.source))
            DiscordCarbot.send_message(
                DiscordCarbot.broadcast_hook_url,
                **LineCarbot.get_file(event.message.id),
            )
        else:
            logger.info('File message ignored as message source {} is not from listening groups.'.format(event.source))

    @handler.default()
    def default(event):
        logger.info('Received unhandled type of event {}.'.format(event))


    @staticmethod
    def user_in_listening_group(user_id):
        for group_id in LineCarbot.listening_groups:
            try:
                # try to get the member profile,
                # if successful then member is in one of the listening groups
                profile = LineCarbot.api.get_group_member_profile(group_id, user_id)
                logger.info('User: {}'.format(profile.display_name))
                return True
            except LineBotApiError:
                pass
       
        return False

    @staticmethod
    def get_sticker_embed(sticker_message):
        """ Retrieves an embeds dict for displaying a sticker message in discord"""
        return { 
            'embeds' : [{
                'image' : {
                    'url' : 'https://stickershop.line-scdn.net/stickershop/v1/sticker/{sticker_id}/{platform}/sticker.png'.format(
                        platform='android',
                        sticker_id=sticker_message.sticker_id
                    )
                }
            }]
        }

    @staticmethod
    def get_file(message_id):
        """ Retrieves file content given message_id. 
        
            Converts and returns a dictionary ready to be passed to Discord API. 
        """
        message_content = LineCarbot.api.get_message_content(message_id)

        def get_ext(mimetype):
            guessed_ext = mimetypes.guess_extension(mimetype)
            if guessed_ext is None:
                if 'audio/' in mimetype:
                    # huge hack, if type is known to be audio then use mp3,
                    # so that discord shows an audio player
                    guessed_ext = '.mp3'
                else:
                    logger.info('Mimetype {} did not have a guessed extension'.format(mimetype))
                    guessed_ext = ''

            if guessed_ext == '.jpe':
                # I don't know why jpe sometimes comes up... Discord can't recognize this.
                # Just use jpg in that case
                guessed_ext = '.jpg'

            return guessed_ext

        filename = 'attachment' + get_ext(message_content.content_type);

        logger.info('Sending {} with message_id={}'.format(filename, str(message_id)))
        return {
            'file' : (filename, functools.reduce(operator.add, message_content.iter_content())),
        }

    @staticmethod
    def get_user_overrides(user_id):
        """ Fetches user's avatar and display name. 
            
            Returns relevant fields to be passed to Discord API so the message
            appears to be sent by the user rather than the bot.

            If user_id is None, there is no override and the avatar and display
            name will fall back to the bot's default. Therefore, the bot's
            default name and avatar should infer 'Unknown User' or the like.
        """
        if user_id is None:
            logger.info('Cannot fetch user_id, user may not have added any bot as a friend.')
            return {}

        profile = LineCarbot.api.get_group_member_profile(settings.LINE['capture_group_id'], user_id)
        
        logger.info('User {} has avatar url {}'.format(profile.display_name, profile.picture_url))

        return { 
            # Discord requires a name to have at least 2 chars,
            # so pad the 'undefined' symbol to a name < 2 characters
            'username'   : '{:\U000e0000^2}'.format(profile.display_name),
            'avatar_url' : profile.picture_url,
        }

@csrf_exempt
def endpoint(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    signature = request.META['HTTP_X_LINE_SIGNATURE']
    body = request.body.decode('utf-8')

    try:
        LineCarbot.handler.handle(body, signature)
    except InvalidSignatureError:
        return HttpResponseForbidden()
    except LineBotApiError:
        return HttpResponseBadRequest()

    return HttpResponse()

