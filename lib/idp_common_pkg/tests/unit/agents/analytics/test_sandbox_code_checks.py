# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Analytics sandbox code reads only the query result and imports only data,
charting and computation modules.

The accepted side is derived from the authority the agent writes from: every Python
example its own prompt carries (``load_python_plot_generation_examples``) must pass.
"""

import json
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from idp_common.agents.analytics.config import load_python_plot_generation_examples
from idp_common.agents.analytics.sandbox_code_checks import (
    SandboxCodeNotAllowedError,
    check_sandbox_code,
)


def _prompt_examples():
    blocks = re.findall(
        r"```python\n(.*?)```", load_python_plot_generation_examples(), re.S
    )
    return blocks


ACCEPTED = [
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\nprint(df.shape)",
        id="read-the-result-file",
    ),
    pytest.param(
        "import numpy as np\nimport json\nprint(json.dumps({'m': float(np.mean([1, 2]))}))",
        id="numpy-and-json",
    ),
    pytest.param(
        "from collections import Counter\nprint(Counter('abc'))", id="from-stdlib"
    ),
    pytest.param(
        "import matplotlib.pyplot as plt\nplt.figure()", id="matplotlib-submodule"
    ),
    pytest.param("print('ok')", id="plain"),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "df['d'] = pd.to_datetime(df['date'])\n"
        "df['n'] = pd.to_numeric(df['c'], errors='coerce')\n"
        "df['t'] = pd.to_timedelta(df['s'], unit='s')",
        id="pandas-converters",
    ),
    pytest.param(
        "import io\nimport matplotlib.pyplot as plt\nplt.savefig(io.BytesIO())",
        id="savefig-to-memory",
    ),
    pytest.param(
        "import io\nimport matplotlib.pyplot as plt\nb = io.BytesIO()\n"
        "plt.savefig(b, format='png')",
        id="savefig-to-a-named-buffer",
    ),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "df.to_csv('out.csv', index=False)",
        id="writer-to-a-local-name",
    ),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "read_count = int(df['read_count'].sum())\nprint(read_count)",
        id="variable-named-like-a-reader",
    ),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "m = df.groupby('k')['v'].sum().reset_index()\n"
        "p = df.pivot_table(index='a', columns='b', values='v', aggfunc='sum')\n"
        "df['d'] = pd.to_datetime(df['d']).dt.strftime('%Y-%m-%d')\n"
        "print(f'{m.v.sum():,.2f}', '{:.1%}'.format(0.5))",
        id="groupby-pivot-dates-formatting",
    ),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "print(df.to_json(orient='records'))\nrows = df.to_dict('records')",
        id="to-methods-without-a-path",
    ),
]

REFUSED = [
    pytest.param("import socket", id="socket"),
    pytest.param("import urllib.request", id="urllib"),
    pytest.param("from http import client", id="http-client"),
    pytest.param("import http.client", id="http-client-dotted"),
    pytest.param("import requests", id="requests"),
    pytest.param("import httpx", id="httpx"),
    pytest.param("import aiohttp", id="aiohttp"),
    pytest.param("import boto3", id="boto3"),
    pytest.param("from botocore.session import Session", id="botocore"),
    pytest.param("import subprocess\nsubprocess.run(['curl', 'x'])", id="subprocess"),
    pytest.param("import os\nos.system('wget x')", id="os"),
    pytest.param("import sys", id="sys"),
    pytest.param("import ssl", id="ssl"),
    pytest.param("import pandas, socket", id="second-name-in-one-import"),
    pytest.param("from . import x", id="relative-import"),
    pytest.param("s = __import__('socket')", id="dunder-import"),
    pytest.param("import importlib", id="importlib-import"),
    pytest.param("exec('import socket')", id="exec"),
    pytest.param("eval('1')", id="eval"),
    pytest.param("import pandas as pd\nf = getattr(pd, 'read_html')", id="getattr"),
    pytest.param("x = ().__class__.__base__.__subclasses__()", id="dunder-attribute"),
    pytest.param("u = 'https://remote.example/x'", id="https-literal"),
    pytest.param("u = b'http://remote.example/x'", id="http-bytes-literal"),
    pytest.param("u = f'https://{host}/x'", id="url-in-f-string"),
    pytest.param(
        "import pandas as pd\npd.read_csv('s3://bucket/key.csv')", id="s3-literal"
    ),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "other = pd.read_csv(df['link'][0])",
        id="reader-with-a-computed-path",
    ),
    pytest.param(
        "import pandas as pd\npd.read_html(filepath_or_buffer=x)", id="reader-keyword"
    ),
    pytest.param("data = open(path).read()", id="open-computed-path"),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "df.to_csv(df['link'][0])",
        id="writer-with-a-computed-path",
    ),
    pytest.param(
        "import matplotlib.pyplot as plt\nplt.savefig(target)", id="savefig-computed"
    ),
    pytest.param("import numpy as np\nnp.loadtxt(p)", id="numpy-loader"),
    pytest.param("import random\nrandom._os.system('id')", id="private-attribute"),
    pytest.param(
        "import typing\ntyping.sys.modules['os'].system('id')", id="module-attribute"
    ),
    pytest.param(
        "import pandas as pd\npd.io.common.os.listdir('.')", id="nested-module-attr"
    ),
    pytest.param(
        "import pandas as pd\npd.read_csv(**{'filepath_or_buffer': u})",
        id="unpacked-keywords",
    ),
    pytest.param("import pandas as pd\npd.read_csv(*args)", id="unpacked-positional"),
    pytest.param("import pandas as pd\npd.ExcelFile(u)", id="excel-file"),
    pytest.param("import pandas as pd\npd.io.common.urlopen(u)", id="urlopen"),
    pytest.param("open('/proc/self/environ')", id="absolute-literal-path"),
    pytest.param("open('../x')", id="parent-literal-path"),
    pytest.param("import io\nio.FileIO(p)", id="fileio"),
    pytest.param("import numpy as np\nnp.memmap(p)", id="memmap"),
    pytest.param("import io\nf = io.open\nf(p)", id="opener-alias"),
    pytest.param("from io import open", id="opener-import"),
    pytest.param(
        "import io\nimport matplotlib.pyplot as plt\nb = io.BytesIO()\nb = target\n"
        "plt.savefig(b)",
        id="buffer-name-rebound",
    ),
    pytest.param(
        "import pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "df.to_csv('/tmp/x.csv')",
        id="writer-absolute-path",
    ),
    pytest.param(
        "import operator, random\noperator.attrgetter('_os')(random).system('id')",
        id="attrgetter",
    ),
    pytest.param(
        "import string, random\n"
        "string.Formatter().get_field('0._os', [random], {})[0].system('id')",
        id="formatter-get-field",
    ),
    pytest.param("import pandas as pd\npd.io.common.get_handle(p, 'r')", id="pd-io"),
    pytest.param("import io\nio.open_code(p)", id="open-code"),
    pytest.param("import numpy as np\nnp.lib.format.open_memmap(p)", id="np-lib"),
    pytest.param("import scipy.io\nscipy.io.loadmat(p)", id="scipy-io-import"),
    pytest.param("from scipy.io import wavfile\nwavfile.read(p)", id="scipy-io-from"),
    pytest.param(
        "import matplotlib.cbook as cb\ncb.to_filehandle(p)", id="matplotlib-cbook"
    ),
    pytest.param("f = open\nf(p)", id="bare-opener-alias"),
    pytest.param("import pandas as pd\npd.HDFStore(p)", id="hdfstore"),
    pytest.param("open('proc/self/environ')", id="relative-path-with-directory"),
    pytest.param(
        "import io\nimport pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "b = io.BytesIO()\nfor b in [target]:\n    df.to_csv(b)",
        id="buffer-rebound-by-for",
    ),
    pytest.param(
        "import io\nimport pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "b = io.BytesIO()\nif (b := target):\n    df.to_csv(b)",
        id="buffer-rebound-by-walrus",
    ),
    pytest.param(
        "import io\nimport pandas as pd\ndf = pd.read_csv('query_results.csv')\n"
        "b = io.BytesIO()\nwith thing() as b:\n    df.to_csv(b)",
        id="buffer-rebound-by-with",
    ),
    pytest.param("!curl remote.example", id="shell-line"),
    pytest.param("%pip install requests", id="notebook-magic"),
    pytest.param("", id="empty"),
]


@pytest.mark.unit
def test_the_prompt_examples_are_found():
    """Non-vacuity: an empty list collects as no cases and the parametrized tests
    would be vacuous, so the derived examples and both hand-written corpora are held
    to a floor."""
    assert len(_prompt_examples()) >= 2
    assert len(ACCEPTED) >= 5
    assert len(REFUSED) >= 20


@pytest.mark.unit
@pytest.mark.parametrize("code", _prompt_examples())
def test_every_prompt_example_is_accepted(code):
    check_sandbox_code(code)


@pytest.mark.unit
@pytest.mark.parametrize("code", ACCEPTED)
def test_accepted(code):
    check_sandbox_code(code)


@pytest.mark.unit
@pytest.mark.parametrize("code", REFUSED)
def test_refused(code):
    with pytest.raises(SandboxCodeNotAllowedError):
        check_sandbox_code(code)


def _tools() -> Any:
    # Typed Any: the type checker reads a Strands tool method's signature with
    # `self` unbound, although the descriptor binds it at run time.
    from idp_common.agents.analytics.tools.code_interpreter_tools import (
        CodeInterpreterTools,
    )

    tools = CodeInterpreterTools(MagicMock(), region="us-east-1")
    tools._invoke_code_interpreter_tool = MagicMock(return_value={"ok": True})
    return tools


@pytest.mark.unit
class TestExecutePython:
    def test_refused_code_is_not_sent_to_the_sandbox(self):
        tools = _tools()

        out = json.loads(tools.execute_python("import socket"))

        assert out["isError"] is True
        assert "socket" in out["error"]
        tools._invoke_code_interpreter_tool.assert_not_called()

    def test_the_description_is_checked_with_the_code(self):
        """The description is prepended as a comment line; a newline in it would
        otherwise start a line of code that was never checked."""
        tools = _tools()

        out = json.loads(
            tools.execute_python("print(1)", description="chart\nimport socket")
        )

        assert out["isError"] is True
        tools._invoke_code_interpreter_tool.assert_not_called()

    def test_accepted_code_is_sent(self):
        tools = _tools()

        tools.execute_python("print(1)")

        call = tools._invoke_code_interpreter_tool.call_args
        assert call.args[0] == "executeCode"
        assert call.args[1]["code"] == "print(1)"
