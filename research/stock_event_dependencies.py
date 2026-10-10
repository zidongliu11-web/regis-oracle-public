"""Date-only source dependencies; the source calendar is never window-clipped."""
from bisect import bisect_left

def potential_split_dependencies(source_calendar,held_dates,actions,lookback=20):
    if lookback<=0 or list(source_calendar)!=sorted(set(source_calendar)):
        raise ValueError('dependency calendar or lookback invalid')
    index={d:i for i,d in enumerate(source_calendar)};potential=[]
    for key,days in held_dates.items():
        try:held=[index[d] for d in days]
        except KeyError as e:raise ValueError('held day outside full source calendar') from e
        for event in actions[key]['split']:
            if event['record_date'] is not None:continue
            loc=bisect_left(source_calendar,event['ex_date'])
            if any(max(0,loc-lookback)<=i<loc for i in held):
                potential.append({'instrument_id':key,'date':event['ex_date'],'kind':'unknown_split_record_may_overlap_recent_actual_holding'})
    return potential
