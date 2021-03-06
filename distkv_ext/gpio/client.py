# command line interface

import anyio
import asyncclick as click
from collections.abc import Mapping

from distkv.util import yprint, attrdict
from distkv.util import res_delete, res_get, res_update, as_service, P

import logging

logger = logging.getLogger(__name__)


@main.group(short_help="Manage GPIO controllers.")  # pylint: disable=undefined-variable
async def cli():
    """
    List GPIO controllers, modify device handling …
    """
    pass


@cli.command()
@click.argument("path", nargs=1)
@click.pass_obj
async def dump(obj, path):
    """Emit the current state as a YAML file.
    """
    res = {}
    path = P(path)
    if len(path) > 4:
        raise click.UsageError("Only up to four path elements allowed")

    async for r in obj.client.get_tree(
        obj.cfg.gpio.prefix + path, nchain=obj.meta, max_depth=4 - len(path)
    ):
        # pl = len(path) + len(r.path)
        rr = res
        if r.path:
            for rp in r.path:
                rr = rr.setdefault(rp, {})
        rr["_"] = r if obj.meta else r.value
    yprint(res, stream=obj.stdout)


@cli.command("list")
@click.argument("path", nargs=1)
@click.pass_obj
async def list_(obj, path):
    """List the next stage.
    """
    res = {}
    path = P(path)
    if len(path) > 4:
        raise click.UsageError("Only up to four path elements allowed")

    res = await obj.client._request(
        action="enumerate", path=obj.cfg.gpio.prefix + path, empty=True
    )
    for r in res.result:
        print(r, file=obj.stdout)


@cli.command("attr")
@click.option("-a", "--attr", multiple=True, help="Attribute to list or modify.")
@click.option("-v", "--value", help="New value of the attribute.")
@click.option("-e", "--eval", "eval_", is_flag=True, help="The value shall be evaluated.")
@click.option("-p", "--path", "path_", is_flag=True, help="The value is a path.")
@click.argument("path", nargs=1)
@click.pass_obj
async def attr_(obj, attr, value, path, eval_, path_):
    """Set/get/delete an attribute on a given GPIO element.

    `--eval` without a value deletes the attribute.
    """
    path = P(path)
    if path_ and eval_:
        raise click.UsageError("split and eval don't work together.")
    if value and not attr:
        raise click.UsageError("Values must have locations ('-a ATTR').")
    if path_:
        value = P(value)
    await _attr(obj, attr, value, path, eval_)


@cli.command()
@click.option("-t", "--type", "typ", help="Port type. 'input' or 'output'.")
@click.option("-m", "--mode", help="Port mode. Use '-' to disable.")
@click.option(
    "-a",
    "--attr",
    nargs=2,
    multiple=True,
    help="One attribute to set (NAME VALUE). May be used multiple times.",
)
@click.argument("path", nargs=1)
@click.pass_obj
async def port(obj, path, typ, mode, attr):
    """Set/get/delete port settings. This is a shortcut for the "attr" command.

    \b
    Known attributes for types+modes:
      input:
        read: dest (path)
        count: read + interval (float), count (+-x for up/down/both)
        button: read + t_bounce (float), t_idle (float), skip (+- ignore noise?),
                       t_clear (float), flow (bool)
      output:
        write: src (path), state (path)
        oneshot: write + t_on (float), state (path)
        pulse:   oneshot + t_off (float)
      *:
        low: bool (signals are active-low if true)

    \b
    Paths elements are separated by spaces.
    "low" is the state of the wire when the input is False.
    Floats may be paths, in which case they're read from there when starting.
    """
    path = P(path)
    if len(path) != 3:
        raise click.UsageError("Path must be 3 elements: host gpioname linenr")
    res = await obj.client.get(obj.cfg.gpio.prefix + path, nchain=obj.meta or 1)
    val = res.get("value", attrdict())

    if type is None:
        raise click.UsageError("Port type is mandatory.")
    if mode is None:
        raise click.UsageError("Port mode is mandatory.")
    attr = (("type", typ), ("mode", mode)) + attr
    for k, v in attr:
        if k == "count":
            if v == "+":
                v = True
            elif v == "-":
                v = False
            elif v in "xX*":
                v = None
            else:
                raise click.UsageError("'%s' wants one of + - X" % (k,))
        elif k in ("low", "skip", "flow"):
            if v == "+":
                v = True
            elif v == "-":
                v = False
            else:
                raise click.UsageError("'%s' wants one of + -" % (k,))
        elif k in {"src", "dest"} or (v is not None and " " in v):
            v = v.split()
        else:
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
        val[k] = v

    await _attr(obj, (), val, path, False, res)


async def _attr(obj, attr, value, path, eval_, res=None):
    # Sub-attr setter. (Or whole-attr-setter if 'attr' is empty.)
    # Special: if eval_ is True, a value of '-' deletes. A mapping replaces instead of updating.
    if res is None:
        res = await obj.client.get(obj.cfg.gpio.prefix + path, nchain=obj.meta or 2)
    try:
        val = res.value
    except AttributeError:
        res.chain = None
    if eval_:
        if value is None:
            value = res_delete(res, attr)
        else:
            value = eval(value)  # pylint: disable=eval-used
            if isinstance(value, Mapping):
                # replace
                value = res_delete(res, attr)
                value = value._update(attr, value=value)
            else:
                value = res_update(res, attr, value=value)
    else:
        if value is None:
            if not attr and obj.meta:
                val = res
            else:
                val = res_get(res, attr)
            yprint(val, stream=obj.stdout)
            return
        value = res_update(res, attr, value=value)
    res = await obj.client.set(
        obj.cfg.gpio.prefix + path, value=value, nchain=obj.meta, chain=res.chain
    )
    if obj.meta:
        yprint(res, stream=obj.stdout)


@cli.command()
@click.argument("name", nargs=1)
@click.argument("controller", nargs=-1)
@click.pass_obj
async def monitor(obj, name, controller):
    """Stand-alone task to monitor a single contoller.

    The first argument must be the local host name.
    """
    from distkv_ext.gpio.task import task
    from distkv_ext.gpio.model import GPIOroot

    server = await GPIOroot.as_handler(obj.client)
    await server.wait_loaded()
    sub = server[name]
    if controller:
        sub = (sub[x] for x in controller)
    async with as_service(obj) as s:
        async with anyio.create_task_group() as tg:
            e = []
            for chip in sub:
                evt = anyio.create_event()
                await tg.spawn(task, chip, evt)
                e.append(evt)
            for evt in e:
                await evt.wait()
            await s.set()
