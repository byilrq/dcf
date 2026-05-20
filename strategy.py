#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Dict, Any, Tuple, List

POSITION_EPSILON = 1e-9


def get_zone(price: float, ma150: float, cfg: Dict[str, Any]) -> str:
    if ma150 is None or ma150 <= 0:
        return 'BOX_ZONE'
    trend_multiple = float(cfg.get('trend_multiple', 1.2))
    sell_multiple = float(cfg.get('sell_multiple', 1.5))
    if price < ma150:
        return 'CHANCE_ZONE'
    if ma150 <= price < ma150 * trend_multiple:
        return 'BOX_ZONE'
    if ma150 * trend_multiple <= price < ma150 * sell_multiple:
        return 'TREND_ZONE'
    return 'SELL_ZONE'


def normalize_position_amount(value: float, mode: str, lot_size: int = 100) -> float:
    value = max(value, 0.0)
    if mode == 'percent':
        return round(value, 6)
    if value <= POSITION_EPSILON:
        return 0.0
    rounded = int(value // lot_size) * lot_size
    if rounded == 0 and value > 0:
        rounded = lot_size
    return float(rounded)


def calculate_pyramid_sell_plan(target_units: float, pyramid_weights: List[float], mode: str, lot_size: int = 100) -> List[Dict[str, Any]]:
    total = max(target_units, 0.0)
    if total <= POSITION_EPSILON:
        return []
    plan = []
    for i, w in enumerate(pyramid_weights):
        units = normalize_position_amount(total * w, mode, lot_size)
        if i == len(pyramid_weights) - 1:
            allocated = sum(p['units'] for p in plan)
            remaining = total - allocated
            units = normalize_position_amount(remaining, mode, lot_size) if remaining > POSITION_EPSILON else 0.0
        if units > POSITION_EPSILON:
            plan.append({'step': i + 1, 'units': units, 'weight_percent': round(w * 100, 1)})
    return plan


def get_pyramid_sell_target_step(price: float, ma150: float, cfg: Dict[str, Any], total_steps: int) -> int:
    if ma150 is None or ma150 <= 0 or total_steps <= 0:
        return 0
    sell_multiple = float(cfg.get('sell_multiple', 1.5))
    current_multiple = price / ma150
    if current_multiple < sell_multiple:
        return 0
    step_pct = float(cfg.get('clear_zone_step_percent', 0.08))
    step = int((current_multiple - sell_multiple) / step_pct) + 1
    return min(max(step, 0), total_steps)


def get_trend_sell_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, mode: str, current_price: float, ma150: float, lot_size: int = 100) -> Tuple[float, Dict[str, Any]]:
    last_trade_price = state.get('last_trade_price', 0.0)
    if last_trade_price <= 0:
        last_trade_price = current_price
    step_pct = float(cfg.get('trend_zone_step_percent', 0.01))
    sell_pct = float(cfg.get('trend_zone_sell_percent', 0.05))
    step_price = last_trade_price * (step_pct if step_pct > 0 else 0.01)
    sell_qty = 0.0
    new_state = state.copy()
    if step_price > 0 and current_price - last_trade_price >= step_price - POSITION_EPSILON:
        excess = max(state.get('current_units', 0.0) - target_units, 0.0)
        if excess > POSITION_EPSILON:
            sell_qty = normalize_position_amount(min(excess, target_units * (sell_pct if sell_pct > 0 else 0.05)), mode, lot_size)
            if sell_qty > POSITION_EPSILON:
                new_state['last_trade_price'] = current_price
    return sell_qty, new_state


def get_pyramid_add_enabled(cfg: Dict[str, Any]) -> str:
    value = str(cfg.get('pyramid_add_enabled', 'auto')).strip().lower()
    return 'yes' if value == 'yes' else 'auto'


def evaluate_pyramid_add_runtime(state: Dict[str, Any], cfg: Dict[str, Any], current_price: float, ma150: float, zone: str, target_units: float) -> Tuple[Dict[str, Any], Dict[str, Any], List[str], str]:
    new_state = state.copy()
    cfg_updates: Dict[str, Any] = {}
    events: List[str] = []
    config_mode = get_pyramid_add_enabled(cfg)
    effective_mode = config_mode

    if config_mode == 'yes' and zone in {'TREND_ZONE', 'SELL_ZONE'}:
        effective_mode = 'auto'
        cfg_updates['pyramid_add_enabled'] = 'auto'
        new_state['pyramid_active'] = False
        new_state['pyramid_step'] = 0
        new_state['target_reached_once'] = False
        events.append('PYRAMID_SWITCH_TO_AUTO')
        return new_state, cfg_updates, events, effective_mode

    if config_mode == 'yes':
        new_state['pyramid_active'] = True
        return new_state, cfg_updates, events, effective_mode

    if zone == 'CHANCE_ZONE':
        effective_mode = 'yes'
        cfg_updates['pyramid_add_enabled'] = 'yes'
        new_state['pyramid_active'] = True
        if not bool(state.get('pyramid_active', False)):
            new_state['pyramid_step'] = 0
            new_state['target_reached_once'] = state.get('current_units', 0.0) >= target_units - POSITION_EPSILON
            new_state['last_add_price'] = current_price
            events.append('PYRAMID_AUTO_TRIGGERED')
    else:
        new_state['pyramid_active'] = False
    return new_state, cfg_updates, events, effective_mode


def get_pyramid_add_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, double_target: float, current_price: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any], str]:
    pyramid_add_step = float(cfg.get('pyramid_add_step', cfg.get('add_box_step', 0.05)))
    pyramid_weights = cfg.get('pyramid_add_weights', cfg.get('pyramid_weights', [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205])) or [1.0]
    total_steps = min(int(cfg.get('pyramid_add_steps', cfg.get('pyramid_steps', len(pyramid_weights)))), len(pyramid_weights))
    pyramid_step = int(state.get('pyramid_step', 0) or 0)
    last_add_price = state.get('last_add_price', current_price) or current_price
    current_units = state.get('current_units', 0.0)
    target_reached_once = bool(state.get('target_reached_once', False))
    new_state = state.copy()

    if current_units >= double_target - POSITION_EPSILON:
        return 0.0, new_state, ''
    if current_units < target_units - POSITION_EPSILON:
        add_qty = normalize_position_amount(target_units - current_units, mode, lot_size)
        if add_qty > POSITION_EPSILON:
            new_state['target_reached_once'] = True
            new_state['last_add_price'] = current_price
            new_state['pyramid_active'] = True
            return add_qty, new_state, 'PYRAMID_INIT'
        return 0.0, new_state, ''
    if target_reached_once and pyramid_step < total_steps and last_add_price > 0:
        if current_price <= last_add_price * (1 - pyramid_add_step) + POSITION_EPSILON:
            add_qty = normalize_position_amount(target_units * pyramid_weights[pyramid_step], mode, lot_size)
            max_allowed = normalize_position_amount(double_target - current_units, mode, lot_size)
            add_qty = normalize_position_amount(min(add_qty, max_allowed), mode, lot_size)
            if add_qty > POSITION_EPSILON:
                new_state['pyramid_step'] = pyramid_step + 1
                new_state['last_add_price'] = current_price
                new_state['pyramid_active'] = True
                return add_qty, new_state, f'PYRAMID_STEP_{pyramid_step + 1}'
    return 0.0, new_state, ''


def get_box_fixed_add_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, current_price: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any]]:
    box_add_step = float(cfg.get('box_add_step', cfg.get('add_box_step', 0.05)))
    add_box_units_pct = float(cfg.get('add_box_units_percent', 0.1))
    last_add_price = state.get('last_add_price', 0.0) or 0.0
    last_trade_price = state.get('last_trade_price', 0.0) or 0.0
    initial_price = state.get('initial_entry_price', 0.0) or 0.0
    current_units = state.get('current_units', 0.0)
    anchor_candidates = [p for p in (last_add_price, last_trade_price, initial_price, current_price) if p and p > 0]
    anchor_price = max(anchor_candidates) if anchor_candidates else current_price
    if current_price > anchor_price * (1 - box_add_step) + POSITION_EPSILON:
        return 0.0, state.copy()
    max_add = normalize_position_amount(target_units - current_units, mode, lot_size)
    if max_add <= POSITION_EPSILON:
        return 0.0, state.copy()
    add_qty = normalize_position_amount(target_units * add_box_units_pct, mode, lot_size)
    add_qty = normalize_position_amount(min(add_qty, max_add), mode, lot_size)
    new_state = state.copy()
    if add_qty > POSITION_EPSILON:
        new_state['last_add_price'] = current_price
    return add_qty, new_state


def get_add_trade_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, double_target: float, current_price: float, ma150: float, zone: str, mode: str, lot_size: int = 100) -> Tuple[float, str, Dict[str, Any], Dict[str, Any], List[str]]:
    new_state, cfg_updates, events, effective_mode = evaluate_pyramid_add_runtime(state, cfg, current_price, ma150, zone, target_units)
    current_units = new_state.get('current_units', 0.0)
    # BOX_ZONE 不再因低于目标仓位而回补；只有 CHANCE_ZONE 触发 auto 后，
    # 才允许按倒金字塔逻辑补到目标仓位并继续加仓。
    if effective_mode == 'yes' and zone in {'CHANCE_ZONE', 'BOX_ZONE'}:
        add_qty, py_state, reason = get_pyramid_add_decision(new_state, cfg, target_units, double_target, current_price, mode, lot_size)
        if add_qty > POSITION_EPSILON:
            merged = new_state.copy(); merged.update(py_state)
            return add_qty, reason, merged, cfg_updates, events
        new_state = py_state
    return 0.0, '', new_state, cfg_updates, events
