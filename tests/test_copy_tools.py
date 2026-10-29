"""What a table's or chart's PNG is called and says, and the HTML a table's PNG is drawn from.

    pytest tests/test_copy_tools.py -v
"""

import os
import sys

import pandas as pd
import plotly.graph_objects as go

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy_tools as ct


def test_titles_of_the_keys_the_page_uses():
    assert ct._table_title("snap_now") == "Line-up now"
    assert ct._table_title("snap_mtd") == "Sailed month-to-date"
    assert ct._table_title("sum_ct_Total") == "Line-up by commodity, week / month / year change: Total"
    assert ct._table_title("sum_sailed_United States") == "Sailed, week / month / year change: United States"
    assert ct._table_title("sum_area_Brazil") == "Line-up by area, week / month / year change: Brazil"
    assert ct._table_title("c_US_ct") == "Line-up, week / month / year change"
    assert ct._table_title("ex_ports") == "Line-up by port, week / month / year change"
    assert ct._table_title("ex_sailed") == "Sailed, week / month / year change"
    assert ct._table_title("c_Brazil_pivot") == "Shipped by month and commodity"


def test_an_unknown_key_still_gives_a_readable_title():
    assert ct._table_title("some_new_table") == "Some new table"


def test_slug_makes_a_file_name():
    assert ct._slug("Line-up over time (vessels)") == "line_up_over_time_vessels"
    assert ct._slug("<b>Corn</b>, Mississippi R.: monthly tonnage") == "corn_mississippi_r_monthly_tonnage"
    assert ct._slug("") == "vessel_lineup"
    assert len(ct._slug("x" * 300)) <= 70


def test_a_chart_is_named_from_its_title_and_dated():
    fig = go.Figure(go.Bar(x=[1], y=[1]))
    fig.update_layout(title="Line-up over time<br><sup>by week</sup>")
    assert ct._fig_title(fig) == "Line-up over time by week"
    assert ct._fig_title(go.Figure()) == ""


def _df():
    return pd.DataFrame({"Commodity": ["Corn", "Total"], "Now": ["10", "12"], "Δ Week": ["+2", "+3"]})


def test_the_png_table_is_the_stylers_when_there_is_one():
    df = _df()
    sty = df.style.apply(lambda r: ["font-weight:700" if r["Commodity"] == "Total" else "" for _ in r], axis=1)
    html = ct._table_html(df, sty, hide_index=True)
    assert "<table" in html and "font-weight: 700" in html          # the bold total carries over
    assert "Corn" in html and "+3" in html
    assert 'class="row_heading' not in html                          # the index is hidden like the on-screen table's
    assert 'class="row_heading' in sty.to_html()                     # and the caller's styler is left as it was


def test_the_png_table_without_a_styler_is_the_plain_frame():
    html = ct._table_html(_df(), None, hide_index=True)
    assert "<table" in html and "Total" in html
    shown_index = ct._table_html(_df().set_index("Commodity"), None, hide_index=False)
    assert ">Corn<" in shown_index


def test_a_table_that_cannot_be_turned_into_html_gives_no_html_rather_than_an_error():
    class Broken:
        def to_html(self, *a, **k):
            raise ValueError("no")
    assert ct._table_html(Broken(), None, hide_index=True) == ""
