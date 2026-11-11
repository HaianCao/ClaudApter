"""Persisted and submitted effort follows the selected protocol/reasoning route."""

import re
import shutil
import subprocess

import pytest

from claudapter.web import _HTML


def test_saved_effort_and_submissions_are_normalized_for_the_route():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the frontend regression harness")
    functions = "\n".join(
        re.search(r"  function " + name + r"\([^\n]+", _HTML).group(0)
        for name in ("reasoningDisabled", "compatibleEffort", "setProviderSaving", "choice")
    )
    initial = re.search(r"  api\('/api/config'\)[^\n]+", _HTML).group(0)
    harness = r"""
const assert=require('node:assert/strict');
const values=['default','low','medium','high','xhigh','max'];
let savingProvider=false,p,state={providers:[],defaults:{}};
const effort={value:'max',options:values.map(value=>({value,disabled:false}))};
const elements={effort,model:{value:'manual-model'},providerForm:{querySelector(){return {}}}};
function $(id){return elements[id]??(elements[id]={})}
function selected(){return p}
function renderProviders(){p=state.providers[0];setProviderSaving(false)}
function api(){return Promise.resolve({providers:[{id:'auto',protocol:'chat',execution_mode:'gateway',upstream_reasoning:'auto'}],defaults:{effort:'max'}})}
function notify(){throw Error('unexpected notification')}
"""
    checks = r"""
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(effort.value,'default');assert(effort.disabled);
  for(const protocol of ['messages','chat','responses']){
    for(const policy of ['auto','provider_default','disabled','openrouter_disabled']){
      p={id:'fixture',protocol,execution_mode:'gateway',upstream_reasoning:policy};state.providers=[p];
      const off=['disabled','openrouter_disabled'].includes(policy)||(policy==='auto'&&protocol!=='messages');
      for(const value of [...values,'invalid']){
        const expected=off||value==='invalid'||(protocol!=='messages'&&['max','xhigh'].includes(value))?'default':value;
        effort.value=value;setProviderSaving(false);
        assert.equal(effort.value,expected);assert.equal(effort.disabled,off);
        assert.equal(choice().effort,expected);
        assert(!effort.options.find(option=>option.value===effort.value).disabled);
        effort.value=value;assert.equal(choice().effort,expected);
        setProviderSaving(true);assert(effort.disabled);assert.throws(choice,/finish saving/);
        setProviderSaving(false);
      }
    }
  }
  p={id:'native',protocol:'messages',execution_mode:'direct',upstream_reasoning:'auto'};
  effort.value='max';setProviderSaving(false);assert.equal(effort.value,'max');assert(!effort.disabled);
  p=null;setProviderSaving(false);assert.equal(effort.value,'default');assert(effort.disabled);
  console.log('passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + functions + "\n" + initial + "\n" + checks,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"
