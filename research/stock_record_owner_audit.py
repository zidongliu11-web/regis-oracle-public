"""Sufficient absence proof for specific record-date dependencies, not full L1."""
def audit_record_owners(potential, held_dates, bindings):
    known={}
    for event in bindings:
        key=(event['instrument_id'],event['source_ex_date'])
        if key in known or not event['record_field_visually_verified']:
            raise ValueError('duplicate or unverified record binding')
        record=event['issuer_record_date']
        if type(record) is not int or not 19000101<=record<event['source_ex_date']:
            raise ValueError('record chronology invalid')
        known[key]=event
    output=[];seen=set()
    for dependency in potential:
        key=(dependency['instrument_id'],dependency['date'])
        if key in seen or key not in known:
            raise ValueError('duplicate or missing actual dependency binding')
        seen.add(key);event=known[key];days=held_dates.get(key[0],[])
        if any(type(day) is not int for day in days) or days!=sorted(set(days)):
            raise ValueError('native holding date identity invalid')
        record_held=event['issuer_record_date'] in days;ex_held=key[1] in days
        # held_dates is logged after entries and before close exits. Presence
        # cannot establish closing ownership, while absence is sufficient.
        resolved=not record_held and not ex_held
        output.append({'instrument_id':key[0],'source_ex_date':key[1],
          'issuer_record_date':event['issuer_record_date'],
          'record_day_in_pre_exit_held_dates':record_held,
          'source_ex_day_in_pre_exit_held_dates':ex_held,
          'specific_dependency_resolved_no_owner':resolved,
          'original_ex_year_conflict_retained':event['original_ex_year_conflict'],
          'scope':'Specific recorded dependency only; no full action coverage or actual fills certification'})
    return output
