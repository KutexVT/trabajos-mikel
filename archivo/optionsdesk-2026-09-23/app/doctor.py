"""Explicit read-only diagnostics; external service probes are opt-in."""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.config import Config, database_path


async def doctor(config: Config, store, probe_external: bool = False) -> dict:
    checks = []

    def add(name, status, detail):
        checks.append({'check': name, 'status': status, 'detail': detail})

    add('Python', 'Listo' if sys.version_info >= (3, 11) else 'Bloqueado', f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')
    for module in ('openai', 'pydantic', 'streamlit', 'pandas', 'ibapi'):
        found = importlib.util.find_spec(module) is not None
        add('Dependencia ' + module, 'Listo' if found else 'Advertencia' if module == 'ibapi' else 'Bloqueado',
            'Instalada' if found else 'Falta SDK oficial TWS API; simulación disponible' if module == 'ibapi' else 'Instala requirements.txt en .venv')
    try:
        Config.model_validate(config.model_dump())
        add('Configuración', 'Listo', 'Validada con límites seguros')
    except ValidationError:
        add('Configuración', 'Bloqueado', 'La configuración no cumple el esquema')

    path = Path(getattr(store, 'path', database_path()))
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=3) as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            version = connection.execute('PRAGMA user_version').fetchone()[0]
        add('SQLite', 'Listo' if integrity == 'ok' else 'Bloqueado', f'Integridad {integrity}; esquema {version}')
    except sqlite3.Error:
        add('SQLite', 'Bloqueado', 'No se pudo verificar SQLite; ejecuta init-db')

    try:
        lock_path = path.with_suffix('.lock')
        with lock_path.open('a+') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                add('Proceso del motor', 'Listo', 'Sin proceso propietario del bloqueo; no hay motor activo en esta DB')
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                add('Proceso del motor', 'Listo', 'Un motor posee el bloqueo; un segundo motor será rechazado')
    except OSError:
        add('Proceso del motor', 'Bloqueado', 'No se pudo verificar el archivo de bloqueo')

    key_present = bool(os.getenv('OPENAI_API_KEY'))
    account_present = bool(os.getenv('IBKR_ACCOUNT'))
    environment = os.getenv('IBKR_ENV', 'paper').lower()
    live_enabled = os.getenv('LIVE_TRADING_ENABLED', '').lower() == 'true'
    add('OPENAI_API_KEY', 'Listo' if key_present else 'Advertencia', f'Presente: {key_present}; valor omitido')
    add('IBKR_ACCOUNT', 'Listo' if account_present else 'Advertencia', f'Presente: {account_present}; valor omitido')
    add('IBKR_ENV', 'Listo' if environment in ('paper', 'live') else 'Bloqueado', environment if environment in ('paper', 'live') else 'Debe ser paper o live')

    control = store.get_control()
    state = store.kv('status', {})
    try:
        heartbeat_age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(state.get('updated_at')).replace('Z', '+00:00'))).total_seconds()
        if not 0 <= heartbeat_age <= 30:
            state = state | {'connected': False, 'risk_healthy': False}
    except (TypeError, ValueError):
        state = state | {'connected': False, 'risk_healthy': False}
    account = state.get('account') or {}
    confirmation = control.get('live_confirmation_until')
    confirmation_current = False
    try:
        confirmation_current = datetime.fromisoformat(str(confirmation).replace('Z', '+00:00')) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        pass
    live_open = (environment == 'live' and live_enabled and confirmation_current
                 and control.get('confirmed_session') == state.get('session_id')
                 and control.get('confirmed_account') == account.get('account_id')
                 and account.get('environment') == 'live' and state.get('connected')
                 and state.get('risk_healthy') and not state.get('critical_alerts')
                 and not control.get('kill_switch'))
    add('Protección Live', 'Advertencia' if live_open else 'Listo',
        'Precondiciones Live presentes; revisar sesión y frescura en motor' if live_open else 'Live bloqueado por precondiciones no satisfechas')
    add('Kill switch', 'Advertencia' if control.get('kill_switch') else 'Listo',
        'Activo y persistente; nuevas aperturas bloqueadas' if control.get('kill_switch') else 'Disponible y persistente; inactivo')
    add('Modo', 'Listo' if control.get('mode', 'OBSERVE') == 'OBSERVE' else 'Advertencia', str(control.get('mode', 'OBSERVE')))

    account_type = account.get('environment', 'unknown')
    account_known = bool(state.get('connected')) and account_type in ('fake', 'paper', 'live')
    add('Cuenta observada', 'Listo' if account_known else 'Advertencia',
        f'Tipo {account_type}; broker simulado' if account_type == 'fake' else f'Tipo {account_type}; estado del último motor' if account_known else 'Sin cuenta conectada identificada; se verificará al conectar IB Gateway')
    last_data = state.get('last_data')
    age = None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(last_data).replace('Z', '+00:00'))).total_seconds()
    except (TypeError, ValueError):
        pass
    fresh = age is not None and 0 <= age <= config.max_data_age_seconds and state.get('connected')
    add('Datos de mercado', 'Listo' if fresh else 'Advertencia',
        'Datos actuales simulados; no verifica suscripciones IBKR' if fresh and account_type == 'fake'
        else 'Datos recientes registrados; revisar data_type y alertas del adaptador' if fresh
        else 'Sin datos actuales; las suscripciones no pueden confirmarse localmente')

    if not probe_external:
        add('Conectividad OpenAI', 'Advertencia', 'No consultada; requiere autorización de cuenta externa y --probe-external')
        add('Conectividad IB Gateway', 'Advertencia', f'No consultada; destino local {config.ibkr_host}:{config.ibkr_port}; usa --probe-external tras autorizar acceso')
    else:
        if key_present:
            try:
                from openai import AsyncOpenAI
                async with AsyncOpenAI(timeout=config.openai_timeout_seconds, max_retries=0) as client:
                    await client.models.retrieve(config.openai_model)
                add('Conectividad OpenAI', 'Listo', 'Modelo accesible; no se generó ninguna decisión')
            except Exception as error:
                add('Conectividad OpenAI', 'Bloqueado', f'Fallo {type(error).__name__}; contenido omitido para proteger secretos')
        else:
            add('Conectividad OpenAI', 'Advertencia', 'No consultada; falta OPENAI_API_KEY')
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(config.ibkr_host, config.ibkr_port), timeout=3)
            writer.close()
            await writer.wait_closed()
            add('Conectividad IB Gateway', 'Listo', 'Puerto TCP local accesible; no prueba autenticación, cuenta ni suscripciones')
        except (OSError, asyncio.TimeoutError):
            add('Conectividad IB Gateway', 'Bloqueado', 'Puerto TCP local no accesible; abre IB Gateway y revisa la configuración API')

    severity = {'Listo': 0, 'Advertencia': 1, 'Bloqueado': 2}
    return {'status': max((item['status'] for item in checks), key=severity.get),
            'external_probes': probe_external, 'checks': checks}
