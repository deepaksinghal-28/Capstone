# License and Attribution

## This capstone project

*Database Server Monitoring and Performance Analysis System* is a final-year
college capstone project. It is a **separate, simplified application**. The
Python code, Streamlit dashboard, monitoring T-SQL, alert rules, tests and
documentation in this folder were written for this project.

## Reference project: SQLMonitor

The design was **inspired by** the open-source project **SQLMonitor** by
Ajay Dwivedi: <https://github.com/imajaydwivedi/SQLMonitor>

SQLMonitor is licensed under the **MIT License**:

> MIT License
>
> Copyright (c) 2022 Ajay Dwivedi
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

### What was taken from SQLMonitor (ideas only)

| SQLMonitor element | How it influenced this project |
|---|---|
| Overall concept: DMV-based monitoring with dashboards and threshold alerts | Same concept, simplified to one local instance, live queries, Streamlit instead of Grafana |
| `Sql-Queries/__DEMO__Blocking-Test.sql` | Idea for a controlled blocking demo (re-written as a short, safe script in a separate test database) |
| `Python-Scripts/connect-2-sqlserver.py` | pyodbc connection pattern (re-written to use Windows Authentication and no stored password) |
| `Python-Scripts/Raise-*Alert-Python.py` | Idea of rule-based alerts with configurable thresholds |
| Grafana dashboards | Layout inspiration (summary cards + charts + detail tables) |

No SQLMonitor source files are copied into this project. If any file is adapted
from SQLMonitor in the future, it will be marked at the top of that file with
the copyright notice above.

## Third-party libraries

Streamlit (Apache-2.0), pyodbc (MIT), pandas (BSD-3-Clause), Plotly (MIT),
python-dotenv (BSD-3-Clause), pytest (MIT). Microsoft SQL Server 2022
Developer Edition is used under Microsoft's free Developer licence (development
and testing only, not production).
