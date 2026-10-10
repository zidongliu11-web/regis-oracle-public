"""Bounded unmodified SDK pagination; silent receive failures are not exhaustion."""
from research.capture_completed_daily_pair import first_response
BLOCK=2000

def collect_pages(rs,fields,*,page_cap=6,row_cap=12000,emit,before_fetch=None):
    if type(page_cap) is not int or not 1<=page_cap<=6 or type(row_cap) is not int or not 1<=row_cap<=12000:
        raise ValueError('pagination_budget_invalid')
    combined=[];cached_next=0;fetch_next=0
    for page_no in range(1,page_cap+1):
        if str(rs.cur_page_num)!=str(page_no) or str(rs.per_page_count)!=str(BLOCK):
            raise ValueError('pagination_header_drift')
        rows=first_response(rs,fields,BLOCK)
        if len(combined)+len(rows)>row_cap:raise ValueError('pagination_rows_cap')
        emit(page_no,rows,{'page':page_no,'rows':len(rows),'error_code':str(rs.error_code),
             'per_page_count':str(rs.per_page_count),'header_day_scope':'initial_API_response_only_SDK_next_does_not_expose_repeat_date_or_fields'})
        combined.extend(rows)
        if len(rows)<BLOCK:
            return combined,{'provider_pagination_terminal':'short_or_empty_page','response_pages':page_no,
                'cached_next_calls':cached_next,'page_fetch_next_calls':fetch_next,'rows':len(combined),
                'provider_pagination_complete':True,'historical_PIT_verified':False,'full_A_verified':False}
        if page_no==page_cap:
            return combined,{'provider_pagination_terminal':'budget_at_full_page','response_pages':page_no,
                'cached_next_calls':cached_next,'page_fetch_next_calls':fetch_next,'rows':len(combined),
                'provider_pagination_complete':False,'historical_PIT_verified':False,'full_A_verified':False}
        # Consume cache through public SDK APIs; never write its cursor or wire.
        if rs.cur_row_num!=0:raise ValueError('pagination_cursor_not_zero')
        for raw in rs.data:
            cached_next+=1
            if not rs.next() or rs.get_row_data()!=raw:raise ValueError('pagination_cached_cursor_drift')
        if rs.cur_row_num!=len(rs.data):raise ValueError('pagination_cursor_not_consumed')
        fetch_next+=1
        if before_fetch:before_fetch({'cached_next_calls':cached_next,'page_fetch_next_calls':fetch_next,'next_page':page_no+1})
        more=rs.next()
        if not more:
            # ResultData.next silently returnsFalse on receiveNone without
            # replacing old data/errorcode. That is not an empty terminal page.
            if str(rs.error_code)!='0' or rs.data or str(rs.cur_page_num)!=str(page_no+1):
                raise ValueError('pagination_false_without_new_empty_terminal')
        elif not rs.data:
            raise ValueError('pagination_true_without_data')
    raise AssertionError('unreachable')
