import asyncio
import inspect
import json
import re
from configparser import ConfigParser
from datetime import datetime
from threading import Thread
from time import mktime
from typing import Union
from urllib import parse

import janus
import requests
from pyrogram.errors import Forbidden, Flood, Unauthorized
from fastapi import FastAPI, Request
from pyrogram import Client, idle, filters
from pyrogram.types import Message, InlineQuery, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from starlette.responses import RedirectResponse

app = FastAPI()


def camel_to_snake(string: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', string).lower()


def normalize_args(args: dict):
    for arg in args.items():
        key = arg[0]
        value = arg[1]

        if key == 'reply_markup':
            value = json.loads(value) if isinstance(value, str) else value
            if isinstance(value, dict) and 'inline_keyboard' in value:
                nrow = 0
                for row in value['inline_keyboard']:
                    nbutton = 0
                    for button in row:
                        row[nbutton] = InlineKeyboardButton(**button)
                        nbutton += 1
                    value['inline_keyboard'][nrow] = row
                    nrow += 1
                value = InlineKeyboardMarkup(value['inline_keyboard'])
            args[key] = {} if value.inline_keyboard == {} else value
        elif value.isnumeric():
            args[key] = int(value)

    return args


def normalize_response(res):
    if isinstance(res, bool):
        return res
    # print(res)
    res = res if isinstance(res, dict) else json.loads(str(res))
    if res['_'] == 'Message':
        res['date'] = mktime(datetime.strptime(res['date'], "%Y-%m-%d %H:%M:%S").timetuple())
        if 'edit_date' in res: res['edit_date'] = mktime(
            datetime.strptime(res['edit_date'], "%Y-%m-%d %H:%M:%S").timetuple())
        if 'forward_date' in res: res['forward_date'] = mktime(
            datetime.strptime(res['forward_date'], "%Y-%m-%d %H:%M:%S").timetuple())
    if 'from_user' in res:
        res['from'] = res['from_user']
    for arg in res.items():
        key = arg[0]
        value = arg[1]
        if isinstance(value, dict):
            res[key] = normalize_response(value)

    return res


class HTTPyro:
    api_id: str
    api_hash: str
    clients: dict = {}
    updates: dict = {}
    updates_queue: dict = {}
    webhooks: dict = {}

    @staticmethod
    def init():
        parser = ConfigParser()
        parser.read('config.ini')
        HTTPyro.api_id = parser.get('httpyro', 'api_id')
        HTTPyro.api_hash = parser.get('httpyro', 'api_hash')

    @staticmethod
    async def get_client(token: str):
        if token not in HTTPyro.clients:
            HTTPyro.updates[token] = []
            HTTPyro.updates_queue[token] = janus.Queue().async_q
            client = HTTPyro.clients[token] = Client(
                session_name=token,
                bot_token=token,
                api_hash=HTTPyro.api_hash,
                api_id=HTTPyro.api_id,
                sleep_threshold=2
            )

            @client.on_message(~filters.me)
            @client.on_inline_query()
            @client.on_callback_query()
            async def handler(client: Client, update: Union[Message, InlineQuery, CallbackQuery]):
                m = re.search('.+\.(.+)\'', str(type(update)))
                botapi_update = {camel_to_snake(m.group(1)): normalize_response(update),
                                 'update_id': 0}  # fixme update_id 0
                await HTTPyro.updates_queue[token].put(
                    botapi_update) if client.bot_token in HTTPyro.webhooks else HTTPyro.updates[token].append(
                    botapi_update)

            await client.start()

            def f():
                idle()
                client.stop()

            thread = Thread(target=f, daemon=True)
            thread.start()
        return HTTPyro.clients[token]


HTTPyro.init()


@app.get("/")
def read_root():
    return RedirectResponse(url='/docs')
    return RedirectResponse(url='http://skrtdev.tk')


def call_method(client: Client, name: str, args):
    if name == 'deleteMessage' or name == 'forwardMessage':
        name += 's'
        args['message_ids'] = int(args['message_id'])

    method = getattr(client, camel_to_snake(name))
    args = normalize_args(args)

    real_args = {}
    for item in inspect.signature(method).parameters.items():
        real_arg = item[0]
        # print(item[1].annotation is int)
        if real_arg in args:
            real_args[real_arg] = int(args[real_arg]) if item[1].annotation is int else args[real_arg]

    return method(**real_args)


@app.get("/bot{token}/getUpdates")
@app.post("/bot{token}/getUpdates")
async def get_updates(token: str, request: Request, timeout: int = 0):
    args = {}
    for param in (await request.form()).items():
        args[param[0]] = param[1]
    if 'timeout' in args:
        timeout = int(args['timeout'])
    if token not in HTTPyro.clients:
        await HTTPyro.get_client(token)
    elapsed = 0
    while (not HTTPyro.updates[token]) and elapsed < timeout:
        t = 0.02
        await asyncio.sleep(t)
        elapsed += t
    res = {'ok': True, 'result': HTTPyro.updates[token]}
    HTTPyro.updates[token] = []
    return res


@app.get("/bot{token}/getWebhookInfo")
@app.post("/bot{token}/getWebhookInfo")
async def get_webhook_info(token: str):
    return {'ok': True, 'result': {'url': ''}}


async def worker(client: Client, url: str, queue):
    while True:
        update = await queue.get()

        print('processed update')
        r = requests.post(url, data=json.dumps(update).encode('utf-8'))
        try:
            res = r.json()
            await call_method(client, res['method'], res)
        except Exception as e:
            print(e)

        queue.task_done()


@app.get("/bot{token}/setWebhook")
@app.post("/bot{token}/setWebhook")
async def set_webhook(token: str, url: str):
    client = await HTTPyro.get_client(token)

    HTTPyro.webhooks[token] = url
    HTTPyro.updates[token] = []

    tasks = []
    for i in range(3):
        task = asyncio.create_task(worker(client, url, HTTPyro.updates_queue[token]))
        tasks.append(task)
    return {'ok': True, 'result': True}


@app.get("/bot{token}/deleteWebhook")
@app.post("/bot{token}/deleteWebhook")
async def delete_webhook(token: str):
    del HTTPyro.webhooks[token]
    del HTTPyro.updates_queue[token]
    HTTPyro.updates[token] = []

    return {'ok': True, 'result': True}


@app.get("/bot{token}/setMyCommands")
@app.post("/bot{token}/setMyCommands")
async def set_bot_commands(token: str, request: Request):
    return {'ok': True, 'result': True}


@app.get("/bot{token}/{method}")
@app.post("/bot{token}/{method}")
async def method(token: str, method: str, request: Request):
    args = {}
    for param in parse.parse_qsl(str(request.query_params)):
        args[param[0]] = param[1]
    for param in (await request.form()).items():
        args[param[0]] = param[1]
    client = await HTTPyro.get_client(token)

    try:
        return {'ok': True, 'result': normalize_response(await call_method(client, method, args))}
    except Exception as e:
        print(e)
        error_code = 400
        if isinstance(e, Forbidden):
            error_code = 403
        elif isinstance(e, Flood):
            error_code = 429
        elif isinstance(e, Unauthorized):
            error_code = 401
        return {'ok': False, 'error_code': error_code, 'description': str(e)}
