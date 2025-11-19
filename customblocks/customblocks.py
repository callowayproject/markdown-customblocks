"""CustomBlocks extension for Python-Markdown."""

import importlib
import inspect
import re
import warnings
from typing import List
from xml.etree import ElementTree as etree  # noqa: N813, S405

from markdown.blockprocessors import BlockProcessor
from markdown.core import Markdown
from markdown.extensions import Extension
from yamlns import namespace as ns

from .entrypoints import load_entry_points_group
from .generators import container

generators_group = "markdown.customblocks.generators"


def _installed_generators() -> List:
    """
    Retrieves and caches the installed generators.

    This function checks for previously cached installed generators. If not cached,
    it loads and caches the installed generators from the specified entry points
    group.

    Returns:
        A list of installed generators.
    """
    if not hasattr(_installed_generators, "value"):
        generators = load_entry_points_group(generators_group)
        _installed_generators.value = generators
    return _installed_generators.value


class CustomBlocksExtension(Extension):
    """CustomBlocks extension for Python-Markdown."""

    def __init__(self, **kwargs):
        self.config = {
            "fallback": [
                container,
                "Renderer used when the type is not defined. By default, is a div container.",
            ],
            "generators": [
                {},
                "Type-renderer bind as a dict, it will update the default map. "
                "Set a type to None to use the fallback.",
            ],
            "config": [
                {},
                "Generators config parameters.",
            ],
        }
        super().__init__(**kwargs)

    def extendMarkdown(self, md: Markdown) -> None:
        """Add CustomBlocks to a Markdown instance."""
        md.registerExtension(self)
        processor = CustomBlocksProcessor(md.parser)
        processor.config = self.getConfigs()
        processor.md = md
        md.parser.blockprocessors.register(processor, "customblocks", 105)


class CustomBlocksProcessor(BlockProcessor):
    """
    Custom processor for handling specific block structures in a Markdown parser.

    This class extends the BlockProcessor class and is designed to detect and process
    custom blocks with specific syntaxes. Blocks are identified by headlines matching
    a predetermined pattern and can contain optional parameters and content. This processor
    parses such blocks, handles their parameters, manages nested content, and invokes
    appropriate callback functions to generate desired output.

    The customization includes:
    - Defining regex to detect headlines, their parameters, and optional closing markers.
    - Parsing the blocks to extract parameters and content.
    - Adapting parameters to match the signature of callbacks.
    - Invoking associated generators for block types to produce output.

    Attributes:
        RE_HEADLINE: Regular expression that detects the block headlines with optional
            parameters and ending.
        RE_PARAM: Regular expression that extracts key-value or keyless parameters
            from parsed headlines.
        RE_END: Regular expression that identifies optional end markers in blocks.
    """

    # Detects headlines
    RE_HEADLINE = re.compile(
        r"(?:^|\n)::: *"  # marker
        r"([\w\-]+)"  # keyword
        r"(?:( |\\\n)+(?:[\w]+=)?("  # params (optional keyword)
        r"'(?:\\.|[^'])*'|"  # single quoted
        r'"(?:\\.|[^"])*"|'  # double quoted
        r"[\S]+"  # single word
        r"))*"
        r"\s*(?:\n|$)"  # ending
    )
    # Extracts every parameter from the headline as (optional) key and value
    RE_PARAM = re.compile(
        r" (?:([\w\-]+)=)?("
        r"'(?:\\.|[^'])*'|"  # single quoted
        r'"(?:\\.|[^"])*"|'  # double quoted
        r"[\S]+"  # single word
        r")"
    )
    # Detect optional end markers
    RE_END = re.compile(r"^:::(?:$|\n)")

    def test(self, parent: etree.Element, block: str) -> re.Match[str] | None:
        """Checks whether a block matches the expected headline format."""
        return self.RE_HEADLINE.search(block)

    def _getGenerator(self, symbolname):
        if callable(symbolname):
            return symbolname
        modulename, functionname = symbolname.split(":", 1)
        module = importlib.import_module(modulename)
        generator = getattr(module, functionname)
        if not callable(generator):
            raise ValueError("{} is not callable".format(symbolname))
        return generator

    def _indentedContent(self, blocks):
        """
        Extracts all the indented content from blocks
        until the first line that is not indented.
        Returns the indented lines removing the indentations.
        """
        content = []
        while blocks:
            block = blocks.pop(0)
            indented, unindented = self.detab(block)
            if indented:
                content.append(indented)
            if unindented:
                blocks.insert(0, unindented)
                break
        return "\n\n".join(content)

    def _processParams(self, params):
        """Parses the block head line to extract parameters,
        Parameters are values consisting on single word o
        double quoted multiple words, that may be preceded
        by a single word key and an equality sign without
        no spaces in between.
        The method returns a tuple of a list with all keyless
        parameters and a dict with all keyword parameters.
        """
        params = params.replace("\\\n", " ")
        args = []
        kwd = {}
        for key, param in self.RE_PARAM.findall(params):
            if param[0] == param[-1] == '"':
                param = eval(param)
            if param[0] == param[-1] == "'":
                param = eval(param)
            if key:
                kwd[key] = param
            else:
                args.append(param)
        return args, kwd

    def _adaptParams(self, callback, ctx, args, kwds):
        """
        Takes args and kwds extracted from custom block head line
        and adapts them to the signature of the callback.
        """

        def warn(message):
            warnings.warn(f"In block '{ctx.type}', " + message)

        signature = inspect.signature(callback)

        # Turn flags into boolean keywords
        for name, param in signature.parameters.items():
            if type(param.default) is not bool and param.annotation is not bool:
                continue

            if name in args:
                args.remove(name)
                kwds[name] = True

            if "no" + name in args:
                args.remove("no" + name)
                kwds[name] = False

        outargs = []
        outkwds = {}
        acceptAnyKey = False
        acceptAnyPos = False
        for name, param in signature.parameters.items():
            if name == "ctx":
                outargs.append(ctx)
                continue
            if param.kind == param.VAR_KEYWORD:
                acceptAnyKey = True
                continue
            if param.kind == param.VAR_POSITIONAL:
                acceptAnyPos = True
                continue

            value = (
                kwds.pop(name)
                if name in kwds and param.kind != param.POSITIONAL_ONLY
                else args.pop(0)
                if args and param.kind != param.KEYWORD_ONLY
                else param.default
                if param.default is not param.empty
                else warn(f"missing mandatory attribute '{name}'") or ""
            )
            if param.kind == param.KEYWORD_ONLY:
                outkwds[name] = value
            else:
                outargs.append(value)

        # Extend var pos
        if acceptAnyPos:
            outargs.extend(args)
        else:
            for arg in args:
                warn(f"ignored extra attribute '{arg}'")
        # Extend var key
        if acceptAnyKey:
            outkwds.update(kwds)
        else:
            for key in kwds:
                warn(f"ignoring unexpected parameter '{key}'")

        return outargs, outkwds

    def _extractHeadline(self, block):
        match = self.RE_HEADLINE.search(block)
        return (
            block[: match.start()],  # pre
            match.group(1),  # type
            block[match.end(1) : match.end()],  # params
            block[match.end() :],  # post
        )

    def run(self, parent, blocks):
        block = blocks[0]
        pre, blocktype, params, post = self._extractHeadline(blocks[0])
        if pre:
            self.parser.parseChunk(parent, pre)
        blocks[0] = post
        args, kwds = self._processParams(params)
        content = self._indentedContent(blocks)
        # Remove optional closing if present
        if blocks:
            blocks[0] = self.RE_END.sub("", blocks[0])

        generators = dict(_installed_generators(), **self.config["generators"])
        generator = self._getGenerator(generators.get(blocktype, container))

        ctx = ns()
        ctx.type = blocktype
        ctx.parent = parent
        ctx.content = content
        ctx.parser = self.parser
        if not hasattr(self.parser.md, "Meta") or not self.parser.md.Meta:
            self.parser.md.Meta = {}
        ctx.metadata = self.parser.md.Meta
        ctx.config = ns(self.config.get("config", {}))

        outargs, kwds = self._adaptParams(generator, ctx, args, kwds)

        result = generator(*outargs, **kwds)

        if result is None:
            return True
        if type(result) is str:
            result = result.encode("utf8")
        if type(result) is bytes:
            result = etree.XML(result)
        parent.append(result)
        return True


def makeExtension(**kwargs):  # pragma: no cover
    return CustomBlocksExtension(**kwargs)


# vim: et ts=4 sw=4
