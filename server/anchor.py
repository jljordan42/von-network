import asyncio
import base64
from datetime import datetime
from enum import IntEnum
import json
import logging
import os
from pathlib import Path
from time import time
from typing import Sequence
import tempfile

import aiohttp
import aiosqlite
import base58
import libnacl

from indy import did, ledger, pool, wallet
from indy.error import ErrorCode, IndyError

LOGGER = logging.getLogger(__name__)

INDY_TXN_TYPES = {
  "0": "NODE",
  "1": "NYM",
  "3": "GET_TXN",
  "100": "ATTRIB",
  "101": "SCHEMA",
  "102": "CRED_DEF",
  "103": "DISCO",
  "104": "GET_ATTR",
  "105": "GET_NYM",
  "107": "GET_SCHEMA",
  "108": "GET_CLAIM_DEF",
  "109": "POOL_UPGRADE",
  "110": "NODE_UPGRADE",
  "111": "POOL_CONFIG",
  "112": "CHANGE_KEY",
}

INDY_ROLE_TYPES = {
  "0": "TRUSTEE",
  "2": "STEWARD",
  "100": "TGB",
  "101": "TRUST_ANCHOR",
}

DEFAULT_PROTOCOL = 2

# Sets the maximum number of transactions to fetch at a time.
MAX_FETCH = int(os.getenv('MAX_FETCH', '50000'))

# Sets the time between transaction fetches (updates); in seconds.
RESYNC_TIME = int(os.getenv('RESYNC_TIME', '120'))

GENESIS_FILE = os.getenv('GENESIS_FILE') or "/home/indy/ledger/sandbox/pool_transactions_genesis"
GENESIS_URL = os.getenv('GENESIS_URL')
GENESIS_VERIFIED = False

ANONYMOUS = os.getenv('ANONYMOUS')
ANONYMOUS = bool(ANONYMOUS and ANONYMOUS != '0' and ANONYMOUS.lower() != 'false')
LEDGER_SEED = os.getenv('LEDGER_SEED')
if not LEDGER_SEED and not ANONYMOUS:
  LEDGER_SEED = '000000000000000000000000Trustee1'

REGISTER_NEW_DIDS = os.getenv('REGISTER_NEW_DIDS', False)
REGISTER_NEW_DIDS = bool(REGISTER_NEW_DIDS and REGISTER_NEW_DIDS != '0' and REGISTER_NEW_DIDS.lower() != 'false')


def is_int(val):
  if isinstance(val, int):
    return True
  if isinstance(val, str) and val.isdigit():
    return True
  return False

def run_coroutine_with_args(coroutine, *args):
  loop = asyncio.new_event_loop()
  asyncio.set_event_loop(loop)
  try:
    return loop.run_until_complete(coroutine(*args))
  except:
    raise
  #finally:
  #  loop.close()

async def _fetch_url(the_url):
  async with aiohttp.ClientSession() as session:
    async with session.get(the_url) as resp:
      r_status = resp.status
      r_text = await resp.text()
      return (r_status, r_text)

async def _fetch_genesis_txn(genesis_url: str, target_path: str) -> bool:
  (r_status, data) = await _fetch_url(genesis_url)

  # check data is valid json
  lines = data.splitlines()
  if not lines or not json.loads(lines[0]):
      raise AnchorException("Genesis transaction file is not valid JSON")

  # write result to provided path
  with open(target_path, "w") as output_file:
      output_file.write(data)
  return True

async def resolve_genesis_file():
  global GENESIS_FILE
  global GENESIS_VERIFIED
  global GENESIS_URL

  if not GENESIS_VERIFIED:
    if not GENESIS_URL and GENESIS_FILE and Path(GENESIS_FILE).exists():
      print("Genesis file already exists:", GENESIS_FILE)
    elif GENESIS_URL:
      f = tempfile.NamedTemporaryFile(mode='w+b', delete=False)
      GENESIS_FILE = f.name
      f.close()
      print("Downloading genesis file from:", GENESIS_URL)
      await _fetch_genesis_txn(GENESIS_URL, GENESIS_FILE)
    else:
      raise AnchorException("No genesis file or URL defined")
    GENESIS_VERIFIED = True

  return GENESIS_FILE

def get_genesis_file():
  global GENESIS_FILE
  return GENESIS_FILE


class LedgerType(IntEnum):
  POOL = 0
  DOMAIN = 1
  CONFIG = 2

  @staticmethod
  def for_value(value):
    if isinstance(value, str):
      if value in '012':
        value = int(value)
      else:
        return LedgerType[value.upper()]
    return LedgerType(value)


class AnchorException(Exception):
  pass

class NotReadyException(AnchorException):
  pass


class AnchorHandle:
  def __init__(self, protocol: str = None):
    self._anonymous = ANONYMOUS
    self._cache = None
    self._did = None
    self._init_error = None
    self._pool = None
    self._protocol = protocol or DEFAULT_PROTOCOL
    self._ready = False
    self._ledger_lock = None
    self._register_dids = REGISTER_NEW_DIDS and not ANONYMOUS
    self._sync_lock = None
    self._syncing = False
    self._wallet = None

  async def _open_pool(self):
    pool_name = 'nodepool'
    pool_cfg = {}
    self._pool = None

    try:
      await pool.set_protocol_version(self._protocol)
    except IndyError as e:
      raise AnchorException("Error setting pool protocol version") from e

    # remove existing pool config by the same name in ledger browser mode
    try:
      await pool.delete_pool_ledger_config(pool_name)
    except IndyError as e:
      if e.error_code != ErrorCode.CommonIOError:
        raise AnchorException("Error deleting existing pool configuration") from e

    try:
      await pool.create_pool_ledger_config(pool_name, json.dumps({
        'genesis_txn': await resolve_genesis_file(),
      }))
    except IndyError as e:
      raise AnchorException("Error creating pool configuration") from e

    try:
      self._pool = await pool.open_pool_ledger(pool_name, json.dumps(pool_cfg))
    except IndyError as e:
      raise AnchorException("Error opening pool ledger connection") from e

  async def _open_wallet(self):
    global LEDGER_SEED
    wallet_cfg = {
      'id': 'trustee_wallet',
      'freshness_time': 0,
      # 'storage_type':
    }
    wallet_access = {'key': 'key'}

    try:
      await wallet.create_wallet(
          config=json.dumps(wallet_cfg),
          credentials=json.dumps(wallet_access))
    except IndyError as e:
      if e.error_code == ErrorCode.WalletAlreadyExistsError:
        print("Wallet already exists")
      else:
        raise AnchorException("Error creating wallet") from e

    try:
      self._wallet = await wallet.open_wallet(
          config=json.dumps(wallet_cfg),
          credentials=json.dumps(wallet_access))
    except IndyError as e:
      raise AnchorException("Error opening wallet") from e

    if LEDGER_SEED:
      try:
        (self._did, verkey) = await did.create_and_store_my_did(self._wallet, json.dumps(
          {'seed': LEDGER_SEED}
        ))
      except IndyError as e:
        if e.error_code == ErrorCode.DidAlreadyExistsError:
          print("DID already exists in wallet")
        else:
          raise AnchorException("Error creating DID in wallet") from e

      if self._did:
        # newly registered DID, set metadata
        try:
          did_meta = {'anchor': True, 'since': int(time())}
          await did.set_did_metadata(self._wallet, self._did, json.dumps(did_meta))
        except IndyError as e:
          raise AnchorException("Error updating DID metadata") from e
      else:
        # find DID in wallet
        dids_with_meta = json.loads(await did.list_my_dids_with_meta(self._wallet))
        for did_with_meta in dids_with_meta:
          meta = json.loads(did_with_meta["metadata"]) if did_with_meta["metadata"] else {}
          if not meta.get("anchor"):
            continue
          self._did, verkey = did_with_meta["did"], did_with_meta["verkey"]
          break
        if not self._did:
          raise AnchorException("Error retrieving existing DID from wallet")

  async def open(self):
    try:
      LEDGER_CACHE_PATH = os.getenv('LEDGER_CACHE_PATH')
      self._cache = LedgerCache(LEDGER_CACHE_PATH)
      await self._cache.open()
      if not self._pool:
        try:
          await self._open_pool()
        except AnchorException:
          self._init_error = "Error initializing pool ledger"
          raise
      if not self._anonymous:
        try:
          await self._open_wallet()
        except AnchorException:
          self._init_error = "Error opening wallet"
          raise
      self._ledger_lock = asyncio.Lock()
      self._sync_lock = asyncio.Lock()
      asyncio.get_event_loop().create_task(self.init_cache())
      self._ready = True
    except Exception as e:
      LOGGER.exception(e)
      raise AnchorException("Initialization error") from e

  async def close(self):
    self._ready = False
    if self._wallet:
      await wallet.close_wallet(self._wallet)
      self._wallet = None
    if self._pool:
      await pool.close_pool_ledger(self._pool)
      self._pool = None
    await self._cache.close()

  @property
  def anonymous(self):
    return self._anonymous

  @property
  def did(self):
    return self._did

  @property
  def pool(self):
    return self._pool

  @property
  def ready(self):
    return self._ready

  @property
  def wallet(self):
    return self._wallet

  async def fetch_tail_txn(self, ledger_type: LedgerType, max_seqno=None):
    async with self._ledger_lock:
      latest = await self._cache.get_latest_seqno(ledger_type)
      latest = latest and latest + 1 or 1
      if max_seqno and latest > max_seqno:
        return
      return await self.get_txn(ledger_type, latest, True, True)

  async def get_latest_seqno(self, ledger_type):
    ledger_type = LedgerType.for_value(ledger_type)
    return await self._cache.get_latest_seqno(ledger_type)

  async def submit_request(self, req_json: str, signed: bool = False):
    try:
      if signed:
        if not self._did:
          raise AnchorException("Cannot sign request: no DID")
        rv_json = await ledger.sign_and_submit_request(
          self._pool, self._wallet, self._did, req_json
        )
      else:
        rv_json = await ledger.submit_request(self._pool, req_json)
      await asyncio.sleep(0)
    except IndyError as e:
      raise AnchorException("Error submitting ledger transaction request") from e

    resp = json.loads(rv_json)
    if resp.get('op', '') in ('REQNACK', 'REJECT'):
      raise AnchorException('Ledger rejected transaction request: {}'.format(resp['reason']))

    return resp

  async def get_nym(self, did: str):
    """
    Fetch a nym from the ledger
    """
    if not self.ready:
      raise NotReadyException()

    get_nym_req = await ledger.build_get_nym_request(self._did, did)
    response = await self.submit_request(get_nym_req, True)
    rv = {}

    data_json = response['result']['data']  # it's double-encoded on the ledger
    if data_json:
        rv = json.loads(data_json)
    return rv

  def _txn2data(self, txn: dict):
    return json.dumps((txn['result'].get('data', {}) or {}).get('txn', {}))

  async def get_txn(self, ledger_type, ident, cache=True, latest=False):
    """
    Fetch a transaction by sequence number or transaction ID
    """
    ledger_type = LedgerType.for_value(ledger_type)
    if not self.ready:
      raise NotReadyException()
    if not ident:
      return None
    if cache:
      txn_info = await self._cache.get_txn(ledger_type, ident)
      if txn_info:
        if latest and is_int(ident):
          await self._cache.set_latest(ledger_type, ident)
        return txn_info
    if not is_int(ident):
      # txn ID must be loaded from cache
      return None

    LOGGER.debug("Fetch %s %s", ledger_type, ident)
    req_json = await ledger.build_get_txn_request(self.did, ledger_type.name, int(ident))
    txn = await self.submit_request(req_json, False)
    txn_data = (txn['result'].get('data', {}) or {})

    if txn_data and txn_data.get('txn'):
      body_json = json.dumps(txn_data, separators=(',',':'), sort_keys=True)
      added = datetime.now() # self._txntime(txn)
      txn_id = None
      if "txnMetadata" in txn_data:
        txn_id = txn_data["txnMetadata"].get("txnId")
      if cache:
        await self._cache.add_txn(ledger_type, ident, txn_id, added, body_json, latest)
      return (ident, txn_id, added, body_json)

  async def get_txn_range(self, ledger_type, start=None, end=None):
    pos = start or 1
    ledger_type = LedgerType.for_value(ledger_type)
    rows = await self._cache.get_txn_range(ledger_type, pos, end)
    if rows:
      pos += len(rows)
    fetch_from = pos
    while not end or fetch_from <= end:
      row = await self.fetch_tail_txn(ledger_type, end)
      if row:
        fetch_from = row[0] + 1
      else:
        break
    if not end or pos <= end:
      rows.extend(await self._cache.get_txn_range(ledger_type, pos, end))
    return rows

  async def get_txn_search(self, ledger_type, query, txn_type=None, limit=-1, offset=0):
    ledger_type = LedgerType.for_value(ledger_type)
    if txn_type is '':
      txn_type = None
    await self.sync_ledger_cache(ledger_type)
    rows, count = await self._cache.get_txn_search(ledger_type, query, txn_type, limit, offset)
    return rows, count

  async def register_did(self, did, verkey, alias=None, role=None):
    """
    Register a DID and verkey on the ledger
    """
    if not self.ready or not self.did:
      raise NotReadyException()

    LOGGER.info('Register agent')
    LOGGER.info("Get nym: %s", did)
    if not await self.get_nym(did):
      LOGGER.info("Send nym: %s/%s", did, verkey)
      req_json = await ledger.build_nym_request(self.did, did, verkey, alias, role)
      await self.submit_request(req_json, True)

  async def seed_to_did(self, seed):
    """
    Resolve a DID and verkey from a seed
    """
    if isinstance(seed, str):
      if len(seed) != 32:
        seed = base64.b64decode(seed)
      else:
        seed = seed.encode('ascii')
    vk, sk = libnacl.crypto_sign_seed_keypair(seed)
    did = base58.b58encode(vk[:16]).decode('ascii')
    verkey = base58.b58encode(vk).decode('ascii')
    return (did, verkey)

  async def init_cache(self):
    LOGGER.info("Syncing ledger cache")
    for ledger_type in LedgerType:
      await self.sync_ledger_cache(ledger_type, True)
    LOGGER.info("Finished sync")
    asyncio.get_event_loop().create_task(self.maintain_cache())

  async def maintain_cache(self):
    while True:
      for ledger_type in LedgerType:
        done = await self.update_ledger_cache(ledger_type)
      await asyncio.sleep(RESYNC_TIME)

  async def update_ledger_cache(self, ledger_type: LedgerType):
    LOGGER.debug("Resyncing ledger cache: %s", ledger_type.name)
    try:
      await self.sync_ledger_cache(ledger_type)
    except asyncio.TimeoutError:
      pass
    LOGGER.debug("Finished resync")

  async def sync_ledger_cache(self, ledger_type: LedgerType, wait=False):
    done = False
    fetched = 0
    # may throw asyncio.TimeoutError
    locked = await asyncio.wait_for(self._sync_lock.acquire(), None if wait else 0.01)
    self._syncing = True
    try:
      latest = await self._cache.get_latest_seqno(ledger_type)
      if latest:
        txn = await self.get_txn(ledger_type, latest, False)
        cache_txn = await self._cache.get_txn(ledger_type, latest)
        if not cache_txn or not txn or json.loads(cache_txn[3]) != json.loads(txn[3]):
          await self._cache.reset()
      while not done:
        row = await self.fetch_tail_txn(ledger_type)
        if row:
          latest = row[0]
          fetched += 1
          if MAX_FETCH > 0 and fetched >= MAX_FETCH:
            LOGGER.debug("%s ledger fetched the maximum number of transaction(s); MAX_FETCH set to %s", ledger_type.name, fetched)
            done = True
        else:
          done = True
    finally:
      self._sync_lock.release()
      self._syncing = False
    if fetched or wait:
      if done:
        LOGGER.info("%s ledger synced with %s transaction(s)", ledger_type.name, latest or 0)
      else:
        LOGGER.info("%s ledger fetched %s transaction(s), incomplete", ledger_type.name, fetched)
    return done

  async def validator_info(self):
    """
    Fetch the status of the validator nodes
    """
    if not self.ready or not self.did:
      raise NotReadyException()

    req_json = await ledger.build_get_validator_info_request(self.did)
    node_data = await self.submit_request(req_json, True)
    node_aliases = list(node_data.keys())
    node_aliases.sort()

    ret = []
    for node in node_aliases:
      reply = json.loads(node_data[node])
      if "result" not in reply:
        continue
      data = reply["result"].get("data")
      data["Node_info"]["Name"] = node
      ret.append(data)
    return ret

  @property
  def public_config(self):
    return {
      "anonymous": self.anonymous,
      "init_error": self._init_error,
      "register_new_dids": self._register_dids,
      "ready": self.ready,
      "syncing": self._syncing,
    }


def txn_extract_terms(txn_json):
  data = json.loads(txn_json)
  result = {}
  type = None
  if data:
    meta = data.get('txnMetadata', {})
    result['txnid'] = meta.get('txnId')
    txn = data.get('txn', {})
    type = txn.get('type')

    meta = txn.get('metadata', {})
    result['sender'] = meta.get('from')

    if type == '1':
      # NYM
      result['ident'] = txn['data']['dest']
      result['alias'] = txn['data'].get('alias')
      short_verkey = None
      if 'verkey' in txn['data']:
        verkey = txn['data']['verkey']
        try:
            did = base58.b58decode(txn['data']['dest'])
            if verkey[0] == "~":
              short_verkey = verkey
              suffix = base58.b58decode(verkey[1:])
              verkey = base58.b58encode(did + suffix).decode('ascii')
            else:
              long = base58.b58decode(verkey)
              if long[0:16] == did:
                short_verkey = '~' + base58.b58encode(long[16:]).decode('ascii')
        except ValueError:
          LOGGER.error("Error decoding verkey: %s", verkey)
        result['short_verkey'] = short_verkey
        result['verkey'] = verkey
      else:
        result['short_verkey'] = None
        result['verkey'] = None
      role_id = txn['data'].get('role')
      result['data'] = INDY_ROLE_TYPES.get(role_id)

    elif type == '100':
      # ATTRIB
      result['ident'] = txn['data']['dest']
      raw_data = txn['data'].get('raw', '{}')
      data = json.loads(raw_data) or {}
      result['alias'] = data.get('endpoint', {}).get('endpoint')

    elif type == '101':
      # SCHEMA
      result['ident'] = '{} {}'.format(txn['data']['data']['name'], txn['data']['data']['version'])
      result['data'] = ' '.join(txn['data']['data']['attr_names'])

    elif type == '102':
      # CRED_DEF
      result['data'] = ' '.join(txn['data']['data']['primary']['r'].keys())

  return type, result


class LedgerCache:
  def __init__(self, db_path: str = None):
    self.db = None
    self.db_path = db_path or ":memory:"

  async def open(self):
    await self.close()
    path = Path(self.db_path)
    LOGGER.info('Ledger cache will be stored in %s', path)
    newDB = not path.exists()
    self.db = await aiosqlite.connect(str(path)).__aenter__()
    if newDB:
      await self.init_db()

  async def close(self):
    if self.db:
      await self.db.close()
      self.db = None

  async def query(self, sql, args=(), *, close=False, script=False):
    result = None
    if not isinstance(sql, str) and isinstance(sql, Sequence):
      for row in sql:
        if result:
          await result.close()
        if isinstance(sql, str) or not isinstance(row, Sequence):
          row = (row,)
        result = await self.query(*row, script=script)
    elif script:
      result = await self.db.executescript(sql)
    else:
      result = await self.db.execute(sql, args)
    if close and result:
      await result.close()
      result = None
    return result

  async def queryone(self, sql, args=()):
    async with await self.query(sql, args) as cursor:
      return await cursor.fetchone()

  async def perform(self, sql, args=(), script=False):
    return await self.query(sql, args, close=True, script=script)

  async def insert(self, sql, args=()):
    async with await self.query(sql, args) as cursor:
      return cursor.lastrowid

  async def init_db(self):
    LOGGER.info("Initializing transaction database")
    await self.perform('''
      CREATE TABLE latest (
        ledger integer PRIMARY KEY,
        seqno integer NOT NULL DEFAULT 0
      );
      CREATE TABLE transactions (
        ledger integer NOT NULL,
        seqno integer NOT NULL,
        txntype integer NOT NULL,
        termsid integer,
        txnid text,
        added timestamp,
        value text,
        PRIMARY KEY (ledger, seqno)
      );
      CREATE INDEX txn_id ON transactions (txnid);
      CREATE VIRTUAL TABLE terms USING fts3(txnid, sender, ident, alias, verkey, short_verkey, data);
      ''', script=True)

  async def reset(self):
    LOGGER.info("Resetting ledger cache")
    await self.perform('''
      TRUNCATE latest;
      TRUNCATE transactions
      ''', script=True)

  async def get_latest_seqno(self, ledger_type: LedgerType):
    row = await self.queryone(
      'SELECT seqno FROM latest WHERE ledger=?', (ledger_type.value,))
    return row and row[0] or None

  async def get_txn(self, ledger_type: LedgerType, ident):
    if not ident:
      return None
    if is_int(ident):
      return await self.queryone(
        'SELECT seqno, txnid, added, value FROM transactions WHERE ledger=? AND seqno=?',
        (ledger_type.value, ident))
    return await self.queryone(
      'SELECT seqno, txnid, added, value FROM transactions WHERE ledger=? AND txnid=?',
      (ledger_type.value, ident))

  async def get_txn_range(self, ledger_type: LedgerType, start=None, end=None):
    latest = await self.get_latest_seqno(ledger_type)
    if start is None:
      start = 1
    if end is None:
      end = latest
    ret = []
    if start and end:
      async with await self.query(
          'SELECT seqno, txnid, added, value FROM transactions ' \
          'WHERE ledger=? AND seqno BETWEEN ? AND ? ORDER BY seqno',
          (ledger_type.value, start, end)) as cursor:
        pos = start
        while True:
          rows = await cursor.fetchmany()
          for row in rows:
            # stop if we encounter a gap
            if row[0] != pos:
              rows = None
              break
            ret.append(row)
            pos += 1
          if not rows:
            break
    return ret

  async def get_txn_search(self, ledger_type: LedgerType, query=None, txn_type=None, limit=-1, offset=0, count=True):
    result = []
    select_fields = 'txn.seqno, txn.txnid, txn.added, txn.value'
    sql = 'SELECT {} FROM terms ' \
      'INNER JOIN transactions txn ON txn.termsid=terms.rowid AND txn.ledger=? ' \
      'WHERE txn.termsid IS NOT NULL'
    params = (ledger_type.value,)
    if query is not None:
      sql += ' AND terms MATCH ?'
      params = (*params, query)
    if txn_type:
      sql += ' AND txn.txntype = ?'
      params = (*params, txn_type)
    select_sql = (sql + ' LIMIT ? OFFSET ?').format(select_fields)
    async with await self.query(select_sql, (*params, limit, offset)) as cursor:
      while True:
        rows = await cursor.fetchmany()
        for row in rows:
          result.append(row)
        if not rows:
          break
    if count:
      count_sql = sql.format('COUNT(*)')
      count_result = await self.queryone(count_sql, params)
      count_val = count_result and count_result[0]
    else:
      count_val = None
    return result, count_val

  async def add_txn(self, ledger_type: LedgerType, seq_no, txn_id, added, value: str, latest=False):
    txn_type, terms = txn_extract_terms(value)
    terms_id = None
    if terms:
      term_names = list(terms.keys())
      upd = 'INSERT INTO terms ({}) VALUES ({})'.format(
        ', '.join(term_names),
        ', '.join('?' for _ in term_names))
      terms_id = await self.insert(upd, tuple(terms[k] for k in term_names))
    await self.insert(
      'INSERT INTO transactions (ledger, seqno, txntype, txnid, added, value, termsid) VALUES (?, ?, ?, ?, ?, ?, ?)',
      (ledger_type.value, seq_no, txn_type, txn_id, added, value, terms_id))
    if latest:
      await self.set_latest(ledger_type, seq_no)

  async def set_latest(self, ledger_type: LedgerType, seq_no):
    await self.perform(
      'REPLACE INTO latest (ledger, seqno) VALUES (?, ?)',
      (ledger_type.value, seq_no))

  async def __aenter__(self) -> "LedgerCache":
      await self.open()
      return self

  async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
      await self.close()
