"""Deterministic policy. No model output can change these limits."""
from __future__ import annotations
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from app.config import Config
from app.models import Proposal, Snapshot, BrokerState, RiskResult
from app.store import TERMINAL


def evaluate(proposal: Proposal, snapshot: Snapshot, state: BrokerState, config: Config,
             control: dict, now: datetime, *, reconciled: bool, critical: list[str],
             opening_count=0, last_opening=None, pending=()) -> RiskResult:
    reasons=[]
    try:
        proposal=Proposal.model_validate(proposal.model_dump(mode='json'))
        state=BrokerState.model_validate(state.model_dump(mode='json'))
        snapshot=Snapshot.model_validate(snapshot.model_dump(mode='json'))
        config=Config.model_validate(config.model_dump())
    except Exception:
        return RiskResult(allowed=False,reasons=['invalid_or_nonfinite_input'])
    if proposal.action=='HOLD': return RiskResult(allowed=False,reasons=['HOLD'])
    candidate=next((c for c in snapshot.candidates if c.candidate_id==proposal.candidate_id),None)
    if candidate is None: return RiskResult(allowed=False,reasons=['unknown_candidate'])
    opening=proposal.action=='OPEN'
    mode=control['mode']
    if mode=='OBSERVE': reasons.append('observe_mode')
    if control['paused']: reasons.append('paused')
    if opening and control['kill_switch']: reasons.append('kill_switch')
    if not reconciled or not state.complete: reasons.append('not_reconciled')
    if not state.connected: reasons.append('broker_disconnected')
    if critical or state.critical_alerts: reasons.append('critical_alert')
    if state.account.currency!='USD' or candidate.currency!='USD': reasons.append('unsupported_currency')
    env=os.environ.get('IBKR_ENV','paper')
    if mode.startswith('PAPER') and (state.account.environment not in {'paper','fake'} or env!='paper'):
        reasons.append('paper_account_mismatch')
    if mode.startswith('LIVE'):
        if state.account.environment!='live' or env!='live': reasons.append('live_account_mismatch')
        expiry=control.get('live_confirmation_until')
        confirmed=bool(expiry and datetime.fromisoformat(expiry)>now and control.get('confirmed_session')==control.get('session_id') and control.get('confirmed_account')==state.account.account_id)
        if os.environ.get('LIVE_TRADING_ENABLED','false').lower()!='true' or not confirmed:
            reasons.append('live_locked')
    if state.account.environment=='unknown': reasons.append('unknown_account')
    if proposal.valid_until<=now or (proposal.valid_until-now).total_seconds()>config.max_proposal_seconds:
        reasons.append('expired_or_invalid_proposal')
    if candidate.symbol!=snapshot.symbol or candidate.symbol not in config.symbols: reasons.append('symbol_not_allowed')
    if candidate.multiplier!=100 or candidate.exchange not in ({'SIM','SMART'} if state.account.environment=='fake' else {'SMART'}): reasons.append('nonstandard_contract')
    if proposal.intent=='BUY_TO_OPEN_CALL' and candidate.right!='CALL': reasons.append('right_mismatch')
    if proposal.intent=='BUY_TO_OPEN_PUT' and candidate.right!='PUT': reasons.append('right_mismatch')
    if not proposal.evidence_ids or not set(proposal.evidence_ids)<=set(snapshot.evidence_ids): reasons.append('unknown_evidence')
    dte=(candidate.expiry-now.astimezone(ZoneInfo('America/New_York')).date()).days
    if dte<=0 or (opening and not config.min_dte<=dte<=config.max_dte): reasons.append('dte_not_allowed')
    for stamp,label in ((candidate.timestamp,'quote'),(state.account.timestamp,'account'),(snapshot.timestamp,'snapshot')):
        age=(now-stamp).total_seconds()
        if age < -2 or age>config.max_data_age_seconds: reasons.append('stale_'+label)
    if candidate.data_type not in {'live','simulated'} or (state.account.environment!='fake' and candidate.data_type!='live'):
        reasons.append('non_live_data')
    if candidate.bid is None or candidate.ask is None or candidate.bid<=0 or candidate.ask<candidate.bid or candidate.mid is None or candidate.mid<=0:
        reasons.append('missing_or_invalid_quote')
    else:
        if abs(candidate.mid-(candidate.ask+candidate.bid)/2)>1e-6: reasons.append('invalid_mid')
        if (candidate.ask-candidate.bid)/candidate.mid>config.max_spread_pct: reasons.append('spread_too_wide')
        if opening and proposal.limit_price>candidate.ask*(1+config.max_quote_change_pct): reasons.append('limit_far_from_market')
        if not opening and proposal.limit_price<candidate.bid*(1-config.max_quote_change_pct): reasons.append('limit_far_from_market')
    if (candidate.volume or 0)<=0 and (candidate.open_interest or 0)<=0: reasons.append('no_liquidity')
    if any(p.quantity<0 for p in state.positions): reasons.append('short_position_in_account')
    active=[o for o in state.orders if o.status not in TERMINAL]
    if any(o.conid==candidate.conid for o in active) or any(x['candidate']['conid']==candidate.conid for x in pending):
        reasons.append('equivalent_pending_order')
    cost=proposal.limit_price*candidate.multiplier*proposal.quantity+config.commission_per_contract*proposal.quantity
    if opening:
        if any(p.symbol==candidate.symbol and p.quantity!=0 for p in state.positions) or any(o.symbol==candidate.symbol for o in active) or any(x['candidate']['symbol']==candidate.symbol for x in pending):
            reasons.append('underlying_already_open')
        if opening_count>=config.max_openings_per_day: reasons.append('daily_opening_limit')
        if last_opening and (now-last_opening).total_seconds()<config.cooldown_minutes*60: reasons.append('cooldown')
        if state.account.daily_pnl<=-config.risk_capital*config.max_daily_loss_pct: reasons.append('daily_loss_limit')
        if cost>config.risk_capital*config.max_trade_risk_pct: reasons.append('trade_risk_limit')
        reserved=sum(x['limit_price']*x['candidate']['multiplier']*x['quantity']+config.commission_per_contract*x['quantity'] for x in pending if x['intent']!='SELL_TO_CLOSE')
        if cost+reserved>state.account.cash: reasons.append('insufficient_cash')
    else:
        available=sum(p.quantity for p in state.positions if p.conid==candidate.conid)
        if proposal.quantity>available: reasons.append('cannot_sell_unowned_options')
    return RiskResult(allowed=not reasons,reasons=reasons or ['allowed'],estimated_cost=cost)
