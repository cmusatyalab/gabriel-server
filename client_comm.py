import logging
import asyncio
import multiprocessing
import queue
from gabriel_server import gabriel_pb2
import websockets
from types import SimpleNamespace


PORT = 9098
NUM_TOKENS = 2
INPUT_QUEUE_MAXSIZE = 2


logger = logging.getLogger(__name__)
websockets_logger = logging.getLogger('websockets')

# The entire payload will be printed if this is allowed to be DEBUG
websockets_logger.setLevel(logging.INFO)


async def _send(websocket, to_client, tokens):
    to_client.num_tokens = tokens
    await websocket.send(to_client.SerializeToString())


async def _send_error(websocket, raw_input, tokens, status):
    from_client = gabriel_pb2.FromClient()
    from_client.ParseFromString(raw_input)

    to_client = gabriel_pb2.ToClient()
    to_client.content.frame_id = from_client.frame_id
    to_client.content.status = status
    await _send(websocket, to_client, tokens)


async def _send_queue_full_message(websocket, raw_input, tokens):
    logger.warn('Queue full')
    await _send_error(websocket, raw_input, tokens,
                      gabriel_pb2.ToClient.Content.Status.QUEUE_FULL)


async def _send_no_tokens_message(websocket, raw_input, tokens):
    logger.error('Client %s sending without tokens', websocket.remote_address)
    await _send_error(websocket, raw_input, tokens,
                      gabriel_pb2.ToClient.Content.Status.NO_TOKENS)

async def _send_engine_not_available_message(websocket, raw_input, tokens):
    logger.warn('Engine Not Available')
    await _send_error(
        websocket, raw_input, tokens,
        gabriel_pb2.FromServer.Status.REQUESTED_ENGINE_NOT_AVAILABLE)

class WebsocketServer:
    def __init__(self, input_queue_maxsize=INPUT_QUEUE_MAXSIZE,
                 num_tokens=NUM_TOKENS):

        # multiprocessing.Queue is process safe
        self.input_queue = multiprocessing.Queue(input_queue_maxsize)

        self.available_engines = set()
        self.num_tokens = num_tokens
        self.clients = {}
        self.event_loop = asyncio.get_event_loop()

    async def consumer_handler(self, websocket, client):
        address = websocket.remote_address

        async for raw_input in websocket:
            logger.debug('Received input from %s', address)
            if client.tokens > 0:
                try:
                    to_from_engine = gabriel_pb2.ToFromEngine()
                    to_from_engine.host = address[0]
                    to_from_engine.port = address[1]
                    to_from_engine.from_client.ParseFromString(raw_input)

                    if to_from_engine.from_client.engine_name in self.clients:
                        client.tokens -= 1

                        # We cannot put the deserialized protobuf in a
                        # multiprocessing.Queue because it cannot be pickled
                        self.input_queue.put_nowait(
                            to_from_engine.SerializeToString())
                    else:
                        await _send_engine_not_available_message(
                            websocket, raw_input, client.tokens)
                except queue.Full:
                    client.tokens += 1

                    await _send_queue_full_message(
                        websocket, raw_input, client.tokens)
            else:
                await _send_no_tokens_message(
                    websocket, raw_input, client.tokens)

    async def producer_handler(self, websocket, client):
        address = websocket.remote_address

        while True:
            content = await client.result_queue.get()

            client.tokens += 1

            to_client = gabriel_pb2.ToClient()
            to_client.content.CopyFrom(content)

            logger.debug('Sending to %s', address)
            await _send(websocket, to_client, client.tokens)

    async def handler(self, websocket, _):
        address = websocket.remote_address
        logger.info('New Client connected: %s', address)

        # asyncio.Queue does not block the event loop
        result_queue = asyncio.Queue()

        client = SimpleNamespace(
            result_queue=result_queue,
            tokens=self.num_tokens)
        self.clients[address] = client

        try:
            consumer_task = asyncio.ensure_future(
                self.consumer_handler(websocket, client))
            producer_task = asyncio.ensure_future(
                self.producer_handler(websocket, client))
            done, pending = await asyncio.wait(
                [consumer_task, producer_task],
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            del self.clients[address]
            logger.info('Client disconnected: %s', address)

    def launch(self):
        start_server = websockets.serve(self.handler, port=PORT)
        self.event_loop.run_until_complete(start_server)
        self.event_loop.run_forever()

    async def queue_result(self, result, address):
        result_queue = self.clients.get(address).result_queue
        if result_queue is None:
            logger.warning('Result for nonexistant address %s', address)
        else:
            await result_queue.put(result)

    def submit_result(self, result, address):
        '''Add a result to self.result_queue.

        Can be called from a different thread. But this thread must be part of
        the same process as the event loop.'''

        asyncio.run_coroutine_threadsafe(
            self.queue_result(result, address),
            self.event_loop)
