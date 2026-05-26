#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Dict, Any, Tuple, List

POSITION_EPSILON = 1e-9


def _parse_position_value(value: Any, default: float = 0.0) -> float:
    """Parse absolute or percent-style position values for validation."""
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return default
            if text.endswith('%'):
                return float(text[:-1]) / 100.0
            return float(text)
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def validate_strategy_config(cfg: Dict[str, Any], name: str = 'strategy') -> None:
    """校验单个标的参数，提前拦截 0、负数、权重异常、目标区间倒置等配置问题。"""
    def positive(key: str, default: Any = None) -> float:
        value = cfg.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{name}: {key} must be a number, got {value!r}')
        if value <= 0:
            raise ValueError(f'{name}: {key} must be > 0, got {value}')
        return value

    base_units = _parse_position_value(cfg.get('base_units', 0), 0.0)
    target_units = _parse_position_value(cfg.get('target_units', 1.0), 1.0)
    if base_units < 0:
        raise ValueError(f'{name}: base_units must be >= 0, got {base_units}')
    if target_units <= 0:
        raise ValueError(f'{name}: target_units must be > 0, got {target_units}')
    limit_target = positive('limit_target', 2.0)
    if limit_target < 1:
        raise ValueError(f'{name}: limit_target must be >= 1, got {limit_target}')
    if base_units * limit_target < target_units - POSITION_EPSILON:
        raise ValueError(
            f'{name}: base_units * limit_target must be >= target_units '
            f'(base={base_units}, limit_target={limit_target}, target={target_units})'
        )

    trend_multiple = positive('trend_multiple', 1.2)
    sell_multiple = positive('sell_multiple', 1.5)
    if trend_multiple >= sell_multiple:
        raise ValueError(f'{name}: trend_multiple must be < sell_multiple')

    positive('trend_zone_step_percent', 0.01)
    positive('trend_zone_sell_percent', 0.05)
    positive('clear_zone_step_percent', 0.08)
    positive('pyramid_add_step', cfg.get('add_box_step', 0.05))
    positive('box_add_step', cfg.get('add_box_step', 0.05))
    positive('add_box_units_percent', 0.1)

    weights = cfg.get('pyramid_add_weights', cfg.get('pyramid_weights'))
    if weights is not None:
        if not isinstance(weights, list) or not weights:
            raise ValueError(f'{name}: pyramid_add_weights must be a non-empty list')
        weights = [float(w) for w in weights]
        if any(w <= 0 for w in weights):
            raise ValueError(f'{name}: pyramid_add_weights must all be > 0')
        if abs(sum(weights) - 1.0) > 1e-6:
            raise ValueError(f'{name}: pyramid_add_weights must sum to 1.0')
        steps = int(cfg.get('pyramid_add_steps', cfg.get('pyramid_steps', len(weights))))
        if steps <= 0 or steps > len(weights):
            raise ValueError(f'{name}: pyramid_add_steps must be in [1, len(weights)]')


def validate_all_strategy_configs(config: Dict[str, Any]) -> None:
    """批量校验整份配置，建议在读取 YAML 后、正式运行策略前调用。"""
    for name, cfg in config.items():
        if isinstance(cfg, dict):
            validate_strategy_config(cfg, name=str(name))


def get_zone(price: float, ma150: float, cfg: Dict[str, Any]) -> str:
    """根据价格与 MA150 的倍数关系，返回 CHANCE/BOX/TREND/CLEAR 四个区域之一。"""
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
    return 'CLEAR_ZONE'


def normalize_position_amount(value: float, mode: str, lot_size: int = 100) -> float:
    """规范化仓位数量：percent 模式保留小数，非 percent 模式按整手 lot_size 处理。"""
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
    """按权重生成分档卖出计划，最后一档自动处理剩余数量。"""
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
    """价格进入 CLEAR_ZONE 后，按 clear_zone_step_percent 计算应卖到第几档。"""
    if ma150 is None or ma150 <= 0 or total_steps <= 0:
        return 0
    sell_multiple = float(cfg.get('sell_multiple', 1.5))
    current_multiple = price / ma150
    if current_multiple < sell_multiple:
        return 0
    step_pct = float(cfg.get('clear_zone_step_percent', 0.08))
    if step_pct <= 0:
        raise ValueError('clear_zone_step_percent must be > 0')
    step = int((current_multiple - sell_multiple) / step_pct) + 1
    return min(max(step, 0), total_steps)


def get_trend_sell_decision(state: Dict[str, Any], cfg: Dict[str, Any], base_units: float, mode: str, current_price: float, ma150: float, lot_size: int = 100) -> Tuple[float, Dict[str, Any]]:
    """趋势区阶梯卖出；只卖出高于 base_units 的机动仓，首次进入趋势区只记录锚点。"""
    new_state = state.copy()
    last_trade_price = state.get('last_trade_price', 0.0)
    if last_trade_price <= 0:
        new_state['last_trade_price'] = current_price
        return 0.0, new_state

    step_pct = float(cfg.get('trend_zone_step_percent', 0.01))
    sell_pct = float(cfg.get('trend_zone_sell_percent', 0.05))
    if step_pct <= 0:
        raise ValueError('trend_zone_step_percent must be > 0')
    if sell_pct <= 0:
        raise ValueError('trend_zone_sell_percent must be > 0')

    sell_qty = 0.0
    if current_price - last_trade_price >= last_trade_price * step_pct - POSITION_EPSILON:
        current_units = state.get('current_units', 0.0)
        excess = max(current_units - base_units, 0.0)
        if excess > POSITION_EPSILON:
            sell_qty = normalize_position_amount(min(excess, current_units * sell_pct), mode, lot_size)
            if sell_qty > POSITION_EPSILON:
                new_state['last_trade_price'] = current_price
    return sell_qty, new_state


def get_pyramid_add_enabled(cfg: Dict[str, Any]) -> str:
    """读取倒金字塔加仓开关；只有 yes 返回 yes，其他值统一视为 auto。"""
    value = str(cfg.get('pyramid_add_enabled', 'auto')).strip().lower()
    return 'yes' if value == 'yes' else 'auto'


def evaluate_pyramid_add_runtime(state: Dict[str, Any], cfg: Dict[str, Any], current_price: float, ma150: float, zone: str, target_units: float) -> Tuple[Dict[str, Any], Dict[str, Any], List[str], str]:
    """维护倒金字塔运行状态：机会区 auto->yes，趋势/Clear区 yes->auto 并重置步数。"""
    new_state = state.copy()
    cfg_updates: Dict[str, Any] = {}
    events: List[str] = []
    config_mode = get_pyramid_add_enabled(cfg)
    effective_mode = config_mode

    if config_mode == 'yes' and zone in {'TREND_ZONE', 'CLEAR_ZONE', 'SELL_ZONE'}:
        effective_mode = 'auto'
        cfg_updates['pyramid_add_enabled'] = 'auto'
        new_state['pyramid_active'] = False
        new_state['pyramid_step'] = 0
        new_state['target_reached_once'] = False
        new_state['pyramid_start_units'] = None
        new_state['pyramid_limit_units'] = None
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
            new_state['pyramid_start_units'] = None
            new_state['pyramid_limit_units'] = None
            events.append('PYRAMID_AUTO_TRIGGERED')
    else:
        new_state['pyramid_active'] = False
    return new_state, cfg_updates, events, effective_mode


def get_pyramid_add_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, limit_units: float, current_price: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any], str]:
    """倒金字塔加仓决策：先补到补仓初始仓位，再从本轮机会区起点逐档加到极限仓位。"""
    pyramid_add_step = float(cfg.get('pyramid_add_step', cfg.get('add_box_step', 0.05)))
    if pyramid_add_step <= 0:
        raise ValueError('pyramid_add_step must be > 0')
    pyramid_weights = cfg.get('pyramid_add_weights', cfg.get('pyramid_weights', [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205])) or [1.0]
    total_steps = min(int(cfg.get('pyramid_add_steps', cfg.get('pyramid_steps', len(pyramid_weights)))), len(pyramid_weights))
    pyramid_step = int(state.get('pyramid_step', 0) or 0)
    last_add_price = state.get('last_add_price', current_price) or current_price
    current_units = state.get('current_units', 0.0)
    new_state = state.copy()

    if limit_units <= target_units - POSITION_EPSILON:
        return 0.0, new_state, ''
    if current_units >= limit_units - POSITION_EPSILON:
        return 0.0, new_state, ''

    # 首次进入机会区，如果当前仓位低于补仓初始仓位，则先一步补到 target_units。
    # 本轮倒金字塔预算随后固定为：极限仓位 - 本轮机会区起点仓位，避免每轮随 current_units 漂移。
    if current_units < target_units - POSITION_EPSILON:
        add_qty = normalize_position_amount(min(target_units - current_units, limit_units - current_units), mode, lot_size)
        if add_qty > POSITION_EPSILON:
            new_state['target_reached_once'] = True
            new_state['last_add_price'] = current_price
            new_state['pyramid_active'] = True
            new_state['pyramid_start_units'] = normalize_position_amount(min(target_units, limit_units), mode, lot_size)
            new_state['pyramid_limit_units'] = normalize_position_amount(limit_units, mode, lot_size)
            return add_qty, new_state, 'PYRAMID_INIT'
        return 0.0, new_state, ''

    if not bool(new_state.get('target_reached_once', False)):
        new_state['target_reached_once'] = True

    try:
        start_units = float(new_state.get('pyramid_start_units') or 0.0)
    except Exception:
        start_units = 0.0
    try:
        limit_anchor = float(new_state.get('pyramid_limit_units') or 0.0)
    except Exception:
        limit_anchor = 0.0
    if start_units <= POSITION_EPSILON or limit_anchor <= POSITION_EPSILON:
        # 当前仓位已达到/超过补仓初始仓位时，从进入机会区当时的 current_units 起算。
        start_units = normalize_position_amount(max(current_units, target_units), mode, lot_size)
        limit_anchor = normalize_position_amount(limit_units, mode, lot_size)
        new_state['pyramid_start_units'] = start_units
        new_state['pyramid_limit_units'] = limit_anchor

    if pyramid_step < total_steps and last_add_price > 0:
        if current_price <= last_add_price * (1 - pyramid_add_step) + POSITION_EPSILON:
            pyramid_budget = max(limit_anchor - start_units, 0.0)
            add_qty = normalize_position_amount(pyramid_budget * pyramid_weights[pyramid_step], mode, lot_size)
            max_allowed = normalize_position_amount(limit_anchor - current_units, mode, lot_size)
            add_qty = normalize_position_amount(min(add_qty, max_allowed), mode, lot_size)
            if add_qty > POSITION_EPSILON:
                new_state['pyramid_step'] = pyramid_step + 1
                new_state['last_add_price'] = current_price
                new_state['pyramid_active'] = True
                return add_qty, new_state, f'PYRAMID_STEP_{pyramid_step + 1}'
    return 0.0, new_state, ''

def get_box_fixed_add_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, current_price: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any]]:
    """BOX 固定补仓；锚点改为 last_add_price 优先，避免历史高价导致连续触发。"""
    box_add_step = float(cfg.get('box_add_step', cfg.get('add_box_step', 0.05)))
    add_box_units_pct = float(cfg.get('add_box_units_percent', 0.1))
    if box_add_step <= 0:
        raise ValueError('box_add_step must be > 0')
    if add_box_units_pct <= 0:
        raise ValueError('add_box_units_percent must be > 0')

    last_add_price = state.get('last_add_price', 0.0) or 0.0
    last_trade_price = state.get('last_trade_price', 0.0) or 0.0
    initial_price = state.get('initial_entry_price', 0.0) or 0.0
    current_units = state.get('current_units', 0.0)
    anchor_price = last_add_price or last_trade_price or initial_price or current_price

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


def get_add_trade_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, limit_units: float, current_price: float, ma150: float, zone: str, mode: str, lot_size: int = 100) -> Tuple[float, str, Dict[str, Any], Dict[str, Any], List[str]]:
    """统一加仓入口；保留原逻辑：机会区启动后，BOX 区仍可延续倒金字塔加仓。"""
    new_state, cfg_updates, events, effective_mode = evaluate_pyramid_add_runtime(state, cfg, current_price, ma150, zone, target_units)
    # BOX_ZONE 不主动从 auto 启动；但 CHANCE_ZONE 触发 auto->yes 后，BOX_ZONE 可继续按 last_add_price 延续加仓。
    if effective_mode == 'yes' and zone in {'CHANCE_ZONE', 'BOX_ZONE'}:
        add_qty, py_state, reason = get_pyramid_add_decision(new_state, cfg, target_units, limit_units, current_price, mode, lot_size)
        if add_qty > POSITION_EPSILON:
            merged = new_state.copy(); merged.update(py_state)
            return add_qty, reason, merged, cfg_updates, events
        new_state = py_state
    return 0.0, '', new_state, cfg_updates, events
