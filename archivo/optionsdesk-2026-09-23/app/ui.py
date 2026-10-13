"""Local control surface. Only validated commands cross into the engine."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from app.config import Config, database_path, load_config
from app.store import Store


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    return value


def age_seconds(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return max(0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return None


def table(store, name, limit=100):
    rows = store.rows(name, limit=limit)
    if not rows:
        st.caption('Sin registros todavía')
        return
    clean = []
    for row in rows:
        payload = decoded(row.get('payload'))
        if isinstance(payload, dict) and name in {'candles', 'candidates', 'fills', 'ai_metrics'}:
            row = {key: value for key, value in row.items() if key != 'payload'} | payload
            if name == 'candidates':
                row['age_seconds'] = age_seconds(row.get('timestamp'))
        item = {}
        for key, value in row.items():
            value = decoded(value)
            item[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        clean.append(item)
    st.dataframe(pd.DataFrame(clean), width='stretch', hide_index=True)


def command(store, action, payload=None):
    number = store.control(action, payload)
    st.success(f'Solicitud {number} registrada · {action}')
    st.caption('El motor valida y audita el resultado. Pausa, desarmado y kill switch bloquean inmediatamente')


def configuration(store, config):
    st.subheader('Límites y estrategia')
    st.caption('Configuración no secreta · La IA no puede modificar estos valores')
    with st.form('configuration'):
        one, two, three = st.columns(3)
        with one:
            symbols = st.text_input('Empresas permitidas', ', '.join(config.symbols), help='Símbolos separados por comas. Máximo 10')
            capital = st.number_input('Capital de riesgo · USD', min_value=1.0, max_value=1000000.0, value=float(config.risk_capital), step=100.0)
            trade_risk = st.number_input('Riesgo por apertura · %', min_value=.001, max_value=1.0, value=config.max_trade_risk_pct * 100, step=.05, format='%.3f')
            daily_loss = st.number_input('Pérdida diaria máxima · %', min_value=.001, max_value=2.0, value=config.max_daily_loss_pct * 100, step=.1, format='%.3f')
        with two:
            openings = st.number_input('Máximo de aperturas por día', min_value=1, max_value=3, value=config.max_openings_per_day)
            minimum = st.number_input('DTE mínimo', min_value=1, max_value=60, value=config.min_dte)
            maximum = st.number_input('DTE máximo', min_value=1, max_value=90, value=config.max_dte)
            spread = st.number_input('Spread máximo · % del mid', min_value=.1, max_value=30.0, value=config.max_spread_pct * 100, step=1.0)
        with three:
            intervals = [5, 6, 10, 12, 15, 20, 30, 60]
            frequency = st.selectbox('Evaluación completa · minutos', intervals, index=intervals.index(config.decision_minutes))
            freshness = st.number_input('Antigüedad máxima · segundos', min_value=1, max_value=60, value=config.max_data_age_seconds)
            cooldown = st.number_input('Cooldown · minutos', min_value=15, max_value=1440, value=config.cooldown_minutes)
            model = st.text_input('Modelo OpenAI', config.openai_model, max_chars=100)
        st.caption('Velas de 1 minuto · Máximo 1 contrato · Solo opciones largas y órdenes límite')
        st.caption('La aprobación humana se configura en Controles mediante PAPER_APPROVAL o LIVE_APPROVAL')
        if st.form_submit_button('Guardar configuración', type='primary'):
            values = config.model_dump()
            values.update(symbols=[symbol.strip().upper() for symbol in symbols.split(',') if symbol.strip()],
                          risk_capital=capital, max_trade_risk_pct=trade_risk / 100,
                          max_daily_loss_pct=daily_loss / 100, max_openings_per_day=openings,
                          min_dte=minimum, max_dte=maximum, max_spread_pct=spread / 100,
                          decision_minutes=frequency, max_data_age_seconds=freshness,
                          cooldown_minutes=cooldown, openai_model=model.strip())
            try:
                valid = Config.model_validate(values)
                command(store, 'config', valid.model_dump(mode='json'))
            except ValidationError as error:
                st.error('Configuración rechazada: ' + '; '.join(item['msg'] for item in error.errors(include_input=False)))


def controls(store, state, status):
    st.subheader('Control del motor')
    a, b, c, d = st.columns(4)
    if a.button('Pausar', width='stretch'):
        command(store, 'pause')
    if b.button('Reanudar', width='stretch', disabled=bool(state.get('kill_switch'))):
        command(store, 'resume')
    if c.button('Desarmar → OBSERVE', width='stretch'):
        command(store, 'disarm')
    if d.button('KILL SWITCH', type='primary', width='stretch'):
        command(store, 'kill')
    st.caption('Kill switch persiste entre reinicios y bloquea nuevas aperturas. No liquida posiciones automáticamente')
    with st.form('mode'):
        modes = ['OBSERVE', 'PAPER_APPROVAL', 'PAPER_AUTO', 'LIVE_APPROVAL', 'LIVE_AUTO']
        selected = st.selectbox('Modo solicitado', modes, index=modes.index(state.get('mode', 'OBSERVE')))
        st.caption('APPROVAL exige aprobar cada propuesta vigente. AUTO aplica igualmente todos los filtros de riesgo')
        if st.form_submit_button('Solicitar cambio de modo'):
            command(store, 'mode', {'mode': selected})
    if state.get('kill_switch'):
        with st.form('clear_kill'):
            checked = st.checkbox('He revisado la causa del bloqueo y quiero retirar el kill switch')
            if st.form_submit_button('Retirar bloqueo'):
                if checked:
                    command(store, 'clear_kill', {'confirmed': True})
                else:
                    st.error('Confirma la revisión antes de retirar el bloqueo')
    st.divider()
    st.subheader('Propuestas pendientes')
    pending = [row for row in store.rows('decisions', limit=100) if row.get('status') == 'awaiting_approval']
    if not pending:
        st.caption('Ninguna propuesta requiere aprobación')
    for row in pending[:10]:
        key = row['key']
        with st.expander(f"{row.get('symbol', 'Opción')} · {key[:16]}"):
            st.json(decoded(row.get('proposal', {})))
            st.caption('La aprobación no evita revalidar vigencia, cotización y riesgo antes del envío')
            accept, reject = st.columns(2)
            if accept.button('Aprobar propuesta', key='approve-' + key):
                command(store, 'approve', {'key': key})
            if reject.button('Rechazar propuesta', key='reject-' + key):
                command(store, 'reject', {'key': key})
    st.subheader('Cancelar orden pendiente')
    final_statuses = {'filled', 'cancelled', 'apicancelled', 'canceled', 'rejected', 'inactive'}
    orders = [row for row in store.rows('orders', limit=100) if str(row.get('status', '')).lower() not in final_statuses]
    if orders:
        with st.form('cancel_order'):
            ref = st.selectbox('Referencia de orden', [row['ref'] for row in orders])
            if st.form_submit_button('Solicitar cancelación'):
                command(store, 'cancel', {'ref': ref})
        st.caption('Una solicitud de cancelación puede llegar después de un fill. El estado del broker decide el resultado')
    else:
        st.caption('No hay órdenes pendientes registradas')
    with st.expander('Habilitación futura de Live'):
        account = status.get('account') or {}
        prerequisites = (os.getenv('IBKR_ENV', 'paper').lower() == 'live'
                         and os.getenv('LIVE_TRADING_ENABLED', '').lower() == 'true'
                         and account.get('environment') == 'live'
                         and status.get('connected')
                         and bool(status.get('session_id')))
        st.warning('Live bloqueado por defecto. La confirmación caduca y pertenece a esta sesión del panel y del motor')
        st.write('Se exige entorno Live explícito, cuenta Live identificada, riesgo sano, datos actuales y ninguna alerta crítica')
        with st.form('confirm_live', clear_on_submit=True):
            phrase = st.text_input('Confirmación escrita', placeholder='ENABLE LIVE <cuenta>', disabled=not prerequisites)
            if st.form_submit_button('Confirmar Live para esta sesión', disabled=not prerequisites):
                expected = 'ENABLE LIVE ' + str(account.get('account_id', ''))
                if phrase != expected:
                    st.error('La frase debe coincidir exactamente con ENABLE LIVE y el identificador de cuenta conectada')
                else:
                    command(store, 'confirm_live', {'phrase': phrase, 'session_id': status['session_id'],
                                                     'ui_session_id': st.session_state.ui_session_id})


@st.fragment(run_every='3s')
def panel():
    with Store(database_path()) as store:
        config = Config.model_validate(store.kv('config', load_config().model_dump()))
        state = store.get_control()
        status = store.kv('status', {})
        heartbeat_age = age_seconds(status.get('updated_at'))
        stale_engine = bool(status) and (heartbeat_age is None or heartbeat_age > 30)
        if stale_engine:
            status = status | {'connected': False, 'risk_healthy': False}
        account = status.get('account') or {}
        active_alerts = [row for row in store.rows('alerts', limit=100) if not row.get('resolved')]
        a, b, c, d = st.columns(4)
        a.metric('Modo', state.get('mode', 'OBSERVE'))
        b.metric('Conexión', 'Conectado' if status.get('connected') else 'Desconectado')
        c.metric('Broker', 'Simulado' if status.get('simulated') else str(status.get('broker', 'Sin iniciar')).upper())
        d.metric('Riesgo', 'Bloqueado' if state.get('kill_switch') else 'Pausado' if state.get('paused') else 'Sano' if status.get('risk_healthy') else 'Sin verificar')
        if state.get('kill_switch'):
            st.error('KILL SWITCH ACTIVO · Nuevas aperturas bloqueadas')
        elif stale_engine:
            st.warning('El motor no actualiza su estado. Los datos mostrados pertenecen a la última sesión registrada')
        elif not status.get('connected'):
            st.info('El panel está listo. Inicia el motor para recibir datos y decisiones')
        elif status.get('simulated'):
            st.info('SIMULACIÓN LOCAL · Datos y ejecuciones generados por el broker de pruebas')
        tabs = st.tabs(['Estado', 'Configuración', 'Mercado', 'Actividad', 'Controles'])
        with tabs[0]:
            left, right = st.columns([2, 1])
            with left:
                st.subheader('Sesión actual')
                st.write('Cuenta: **' + str(account.get('account_id', 'Sin identificar')) + '**')
                st.write('Entorno: **' + str(account.get('environment', 'Sin verificar')) + '**')
                st.write('Último dato: ' + str(status.get('last_data') or 'Sin datos'))
                st.write('Próxima evaluación: ' + str(status.get('next_evaluation') or 'Esperando velas'))
                age = age_seconds(status.get('last_data'))
                if age is not None:
                    st.caption(f'Antigüedad del último dato: {age:.0f} s · Límite: {config.max_data_age_seconds} s')
                st.subheader('Última decisión')
                latest = status.get('last_decision')
                if latest:
                    st.json(decoded(latest))
                else:
                    rows = store.rows('decisions', limit=1)
                    if rows:
                        st.json(decoded(rows[0].get('proposal', {})))
                    else:
                        st.caption('Esperando la primera evaluación. HOLD también es una decisión')
            with right:
                st.subheader('Cuenta · USD')
                for label, key in [('Efectivo', 'cash'), ('Patrimonio', 'net_liquidation'), ('PnL realizado', 'realized_pnl'), ('PnL no realizado', 'unrealized_pnl'), ('PnL diario', 'daily_pnl')]:
                    value = account.get(key)
                    st.metric(label, f'{value:,.2f}' if isinstance(value, (int, float)) else '—')
            st.subheader('Alertas activas')
            if active_alerts:
                for alert in active_alerts[:10]:
                    display = st.error if str(alert.get('severity', '')).lower() == 'critical' else st.warning
                    display(str(alert.get('message', 'Alerta sin mensaje')))
            else:
                st.caption('Sin alertas registradas')
        with tabs[1]:
            configuration(store, config)
        with tabs[2]:
            st.subheader('Universo permitido')
            st.write(' · '.join(config.symbols))
            st.caption(f'Opciones largas · DTE {config.min_dte}–{config.max_dte} · Spread máximo {config.max_spread_pct:.0%}')
            st.subheader('Velas de un minuto')
            candles = [decoded(row.get('payload')) for row in store.rows('candles', limit=300)]
            if candles:
                frame = pd.DataFrame(candles)
                frame['close_time'] = pd.to_datetime(frame['close_time'], utc=True)
                prices = frame.pivot_table(index='close_time', columns='symbol', values='close').sort_index()
                st.line_chart(prices, height=260, x_label='Cierre UTC', y_label='Precio USD')
            table(store, 'candles')
            st.subheader('Candidatos de opciones')
            table(store, 'candidates')
            st.subheader('Snapshots y calidad de datos')
            table(store, 'snapshots', limit=20)
        with tabs[3]:
            for label, name in [('Propuestas y rechazos', 'decisions'), ('Órdenes', 'orders'), ('Fills', 'fills'), ('Posiciones observadas', 'positions'), ('Métricas de IA', 'ai_metrics'), ('Auditoría', 'events')]:
                with st.expander(label, expanded=name == 'decisions'):
                    table(store, name)
        with tabs[4]:
            controls(store, state, status)
        st.caption('Actualización cada 3 s · Tiempos en UTC · Las solicitudes se validan de nuevo en el motor')


def main():
    st.set_page_config(page_title='Options Desk · Local', page_icon='◈', layout='wide')
    st.markdown('''<style>
    .stApp {background:#0b111b;color:#e6edf7}
    [data-testid="stHeader"] {background:#0b111b}
    [data-testid="stMetric"] {background:#131e2e;border:1px solid #27354a;border-radius:12px;padding:18px}
    [data-testid="stMetricLabel"] {color:#a4b5d0}
    h1,h2,h3 {letter-spacing:-.03em}
    .stTabs [data-baseweb="tab-list"] {gap:28px}
    </style>''', unsafe_allow_html=True)
    st.caption('IBKR / LOCAL WORKSPACE')
    st.title('Options Desk')
    st.write('Mercado continuo · Decisiones auditadas · Riesgo determinista')
    if 'ui_session_id' not in st.session_state:
        st.session_state.ui_session_id = str(uuid4())
    try:
        panel()
    except (OSError, ValueError) as error:
        st.error(f'No se pudo iniciar el panel: {type(error).__name__}. Ejecuta python -m app doctor')


if __name__ == '__main__':
    main()
