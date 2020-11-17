''' Implements the language server for YARA '''
import asyncio
from copy import deepcopy
from itertools import chain
import json
import logging
from pathlib import Path
import re

from .base import protocol as lsp
from .base.server import LanguageServer
from .base import errors as ce
from . import helpers

try:
    import yara
    HAS_YARA = True
except ModuleNotFoundError:
    HAS_YARA = False
    # cannot notify user at this point unfortunately - no clients have connected
    logging.warning("yara-python is not installed. Diagnostics and Compile commands are disabled")


class YaraLanguageServer(LanguageServer):
    ''' Implements the language server for YARA '''
    def __init__(self):
        ''' Handle the particulars of the server's YARA implementation '''
        super().__init__()
        self._logger = logging.getLogger("yara")
        # variable symbols have a few possible first characters
        self._varchar = ["$", "#", "@", "!"]
        self.diagnostics_warned = False
        self.hover_langs = [lsp.MarkupKind.Markdown, lsp.MarkupKind.Plaintext]
        schema = Path(__file__).parent.joinpath("data", "modules.json").resolve()
        self.modules = json.loads(schema.read_text())
        self.workspace = False

    def _get_document(self, file_uri: str, dirty_files: dict) -> str:
        ''' Return the document text for a given file URI either from disk or memory '''
        if file_uri in dirty_files:
            return dirty_files[file_uri]
        file_path = helpers.parse_uri(file_uri, encoding=self._encoding)
        with open(file_path, "r") as rule_file:
            return rule_file.read()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        '''React and respond to client messages

        :reader: asyncio StreamReader. The connected client will write to this stream
        :writer: asyncio.StreamWriter. The connected client will read from this stream
        '''
        config = {}
        # file_uri => contents
        dirty_files = {}
        has_started = False
        self._logger.info("Client connected")
        self.num_clients += 1
        while True:
            try:
                if reader.at_eof():
                    self._logger.warning("Client has closed")
                    self.num_clients -= 1
                    break
                elif self.num_clients <= 0:
                    # clear out memory
                    dirty_files.clear()
                    # remove connected clients
                    await self.remove_client(writer)
                message = await self.read_request(reader)
                # this matches some kind of JSON-RPC message
                if "jsonrpc" in message:
                    method = message.get("method", "")
                    self._logger.debug("Client sent a '%s' message", method)
                    # if an id is present, this is a JSON-RPC request
                    if "id" in message:
                        if not has_started and method == "initialize":
                            rootdir = helpers.parse_uri(message["params"]["rootUri"], encoding=self._encoding)
                            if rootdir:
                                self.workspace = Path(rootdir)
                                self._logger.info("Client workspace folder: %s", self.workspace)
                            else:
                                self._logger.info("No client workspace specified")
                                self.workspace = False
                            client_options = message.get("params", {}).get("capabilities", {})
                            announcement = self.initialize(client_options)
                            await self.send_response(message["id"], announcement, writer)
                        elif has_started and method == "shutdown":
                            self._logger.info("Client requested shutdown")
                            await self.send_response(message["id"], {}, writer)
                            # explicitly clear the dirty files on shutdown
                            dirty_files.clear()
                        elif has_started and method == "textDocument/completion":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", None)
                            if file_uri:
                                document = self._get_document(file_uri, dirty_files)
                                completions = await self.provide_code_completion(message["params"], document)
                                await self.send_response(message["id"], completions, writer)
                        elif has_started and method == "textDocument/definition":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", None)
                            if file_uri:
                                document = self._get_document(file_uri, dirty_files)
                                definition = await self.provide_definition(message["params"], document)
                                await self.send_response(message["id"], definition, writer)
                        # elif has_started and method == "textDocument/documentHighlight":
                        #     highlights = await self.provide_highlight(message["params"])
                        #     await self.send_response(message["id"], highlights, writer)
                        elif has_started and method == "textDocument/hover":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", None)
                            if file_uri:
                                document = self._get_document(file_uri, dirty_files)
                                hovers = await self.provide_hover(message["params"], document)
                                await self.send_response(message["id"], hovers, writer)
                        elif has_started and method == "textDocument/references":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", None)
                            if file_uri:
                                document = self._get_document(file_uri, dirty_files)
                                references = await self.provide_reference(message["params"], document)
                                await self.send_response(message["id"], references, writer)
                        elif has_started and method == "textDocument/rename":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", None)
                            if file_uri:
                                document = self._get_document(file_uri, dirty_files)
                                renames = await self.provide_rename(message["params"], document, file_uri)
                                await self.send_response(message["id"], renames, writer)
                        elif has_started and method == "workspace/executeCommand":
                            response = await self.execute_command(message["params"], dirty_files, writer)
                            await self.send_response(message["id"], response, writer)
                    # if no id is present, this is a JSON-RPC notification
                    else:
                        if method == "initialized":
                            self._logger.info("Client has been successfully initialized")
                            has_started = True
                            params = {"type": lsp.MessageType.INFO, "message": "Successfully connected"}
                            await self.send_notification("window/showMessageRequest", params, writer)
                        elif has_started and method == "exit":
                            # first remove the client associated with this handler
                            await self.remove_client(writer)
                            raise ce.ServerExit("Server exiting process per client request")
                        elif has_started and method == "workspace/didChangeConfiguration":
                            config = message.get("params", {}).get("settings", {}).get("yara", {})
                            self._logger.debug("Changed workspace config to %s", json.dumps(config))
                        elif has_started and method == "textDocument/didChange":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", None)
                            if file_uri:
                                self._logger.debug("Adding %s to dirty files list", file_uri)
                                for changes in message.get("params", {}).get("contentChanges", []):
                                    # full text is submitted with each change
                                    change = changes.get("text", None)
                                    if change:
                                        dirty_files[file_uri] = change
                        elif has_started and method == "textDocument/didClose":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", "")
                            # file is no longer dirty after closing
                            if file_uri in dirty_files:
                                del dirty_files[file_uri]
                                self._logger.debug("Removed %s from dirty files list", file_uri)
                        elif has_started and method == "textDocument/didSave":
                            file_uri = message.get("params", {}).get("textDocument", {}).get("uri", "")
                            # file is no longer dirty after saving
                            if file_uri in dirty_files:
                                del dirty_files[file_uri]
                                self._logger.debug("Removed %s from dirty files list", file_uri)
                            if config.get("compile_on_save", False):
                                file_path = helpers.parse_uri(file_uri)
                                with open(file_path, "rb") as ifile:
                                    document = ifile.read().decode(self._encoding)
                                diagnostics = await self.provide_diagnostic(document)
                            else:
                                diagnostics = []
                            params = {
                                "uri": file_uri,
                                "diagnostics": diagnostics
                            }
                            await self.send_notification("textDocument/publishDiagnostics", params, writer)
            except ce.NoYaraPython as warn:
                self._logger.warning(warn)
                params = {
                    "type": lsp.MessageType.WARNING,
                    "message": warn
                }
                await self.send_notification("window/showMessage", params, writer)
            except (ce.CodeCompletionError, ce.DefinitionError, ce.DiagnosticError, ce.HighlightError, \
                    ce.HoverError, ce.RenameError, ce.SymbolReferenceError) as err:
                self._logger.error(err)
                params = {
                    "type": lsp.MessageType.ERROR,
                    "message": str(err)
                }
                await self.send_notification("window/showMessage", params, writer)

    def initialize(self, client_options: dict) -> dict:
        '''Announce language support methods

        :client_options: Dictionary of registration options that the client supports
        '''
        doc_options = client_options.get("textDocument", {})
        ws_options = client_options.get("workspace", {})
        server_options = {}
        if doc_options.get("completion", {}).get("dynamicRegistration", False):
            server_options["completionProvider"] = {
                # The server does not provide support to resolve additional information for a completion item
                "resolveProvider": False,
                "triggerCharacters": ["."]
            }
        if doc_options.get("definition", {}).get("dynamicRegistration", False):
            server_options["definitionProvider"] = True
        # if doc_options.get("documentHighlight", {}).get("dynamicRegistration", False):
        #     server_options["documentHighlightProvider"] = True
        if doc_options.get("hover", {}).get("dynamicRegistration", False):
            server_options["hoverProvider"] = True
            self.hover_langs = doc_options.get("hover", {}).get("contentFormat", self.hover_langs)
        if ws_options.get("executeCommand", {}).get("dynamicRegistration", False):
            server_options["executeCommandProvider"] = {
                "commands": []
            }
            if HAS_YARA:
                server_options["executeCommandProvider"]["commands"].append("yara.CompileRule")
                server_options["executeCommandProvider"]["commands"].append("yara.CompileAllRules")
        # if doc_options.get("formatting", {}).get("dynamicRegistration", False):
        #     server_options["documentFormattingProvider"] = True
        if doc_options.get("references", {}).get("dynamicRegistration", False):
            server_options["referencesProvider"] = True
        if doc_options.get("rename", {}).get("dynamicRegistration", False):
            server_options["renameProvider"] = True
        if doc_options.get("synchronization", {}).get("dynamicRegistration", False):
            # Documents are synced by always sending the full content of the document
            server_options["textDocumentSync"] = lsp.TextSyncKind.FULL
        return {"capabilities": server_options}

    async def execute_command(self, params: dict, dirty_files: dict, writer: asyncio.StreamWriter) -> dict:
        '''Execute the specified command

        Returns any results from the command
        '''
        response = { "result": None }
        cmd = params.get("command", "")
        args = params.get("arguments", [])
        try:
            if cmd == "yara.CompileRule":
                self._logger.info("Compiling rule per user's request")
            elif cmd == "yara.CompileAllRules":
                for result in await self._compile_all_rules(dirty_files, self.workspace):
                    await self.send_notification("textDocument/publishDiagnostics", result, writer)
                # done with diagnostics - nothing needs to be returned
            else:
                self._logger.warning("Unknown command: %s [%s]", cmd, ",".join(args))
        except (ce.DiagnosticError,) as err:
            # only add an error code if we see one
            response = {
                "result": None,
                "error": lsp.ResponseError.convert_exception(err)
            }
        return response

    async def _compile_all_rules(self, dirty_files: dict, workspace=None) -> list:
        # temp copy of filenames => contents
        # do a deep copy in order to not mess with dirty file contents
        diagnostics = []
        documents = deepcopy(dirty_files)
        if workspace:
            self._logger.info("Compiling all rules in %s per user's request", workspace)
            for file in chain(workspace.glob("**/*.yara"), workspace.glob("**/*.yar")):
                file_uri = file.as_uri()
                documents[file_uri] = self._get_document(file_uri, dirty_files)
        else:
            self._logger.warning("No workspace specified. CompileAllRules will only work on open docs")
            self._logger.info("Compiling all unsaved files per user's request")
            documents = dirty_files
        # documents should be a list of file contents
        for file_uri, document in documents.items():
            diagnostic = await self.provide_diagnostic(document)
            if diagnostic:
                diagnostics.append({
                    "uri": file_uri,
                    "diagnostics": diagnostic
                })
        return diagnostics

    async def provide_code_completion(self, params: dict, document: str) -> list:
        '''Respond to the completionItem/resolve request

        Returns a (possibly empty) list of completion items
        '''
        try:
            results = []
            trigger = params.get("context", {}).get("triggerCharacter", ".")
            # typically the trigger is at the end of a line, so subtract one to avoid an IndexError
            pos = lsp.Position(line=params["position"]["line"], char=params["position"]["character"]-1)
            symbol = helpers.resolve_symbol(document, pos)
            if not symbol:
                return []
            # split up the symbols into component parts, leaving off the last trigger character
            symbols = symbol.split(trigger)
            schema = self.modules
            for depth, symbol in enumerate(symbols):
                if symbol in schema:
                    # if we're at the last symbol, return completion items
                    if depth == len(symbols) - 1:
                        completion_items = schema.get(symbol, {})
                        if isinstance(completion_items, dict):
                            for label, kind_str in completion_items.items():
                                kind = lsp.CompletionItemKind.CLASS
                                if str(kind_str).lower() == "enum":
                                    kind = lsp.CompletionItemKind.ENUM
                                elif str(kind_str).lower() == "property":
                                    kind = lsp.CompletionItemKind.PROPERTY
                                elif str(kind_str).lower() == "method":
                                    kind = lsp.CompletionItemKind.METHOD
                                results.append(lsp.CompletionItem(label, kind))
                    else:
                        schema = schema[symbol]
            return results
        except Exception as err:
            self._logger.error(err)
            raise ce.CodeCompletionError("Could not offer completion items: {}".format(err))

    async def provide_definition(self, params: dict, document: str) -> list:
        '''Respond to the textDocument/definition request

        Returns a (possibly empty) list of symbol Locations
        '''
        results = []
        try:
            # the try/except statement after this uses the 'symbol' variable in the exception block
            # so we need to separate the code before 'symbol' is instantiated from the code after
            # there's probably a better way to do this
            file_uri = params.get("textDocument", {}).get("uri", None)
            line = params.get("position", {}).get("line", None)
            char = params.get("position", {}).get("character", None)
            pos = lsp.Position(line=line, char=char)
            symbol = helpers.resolve_symbol(document, pos)
            if not symbol:
                return []
        except Exception as err:
            self._logger.error(err)
            raise ce.DefinitionError("Could not find symbol for definition request")
        try:
            # check to see if the symbol is a variable or a rule name (currently the only valid symbols)
            if symbol[0] in self._varchar:
                pattern = "\\${} =\\s".format("".join(symbol[1:]))
                rule_range = helpers.get_rule_range(document, pos)
                match_lines = document.split("\n")[rule_range.start.line:rule_range.end.line+1]
                rel_offset = rule_range.start.line
                # ignore the "$" variable identifier at the beginning of the match
                char_start_offset = 1
            # else assume this is a rule symbol
            else:
                pattern = "\\brule {}\\b".format(symbol)
                match_lines = document.split("\n")
                rel_offset = 0
                # ignore the "rule " string at the beginning of the match
                char_start_offset = 5

            for index, line in enumerate(match_lines):
                for match in re.finditer(pattern, line):
                    if match:
                        offset = rel_offset + index
                        locrange = lsp.Range(
                            start=lsp.Position(line=offset, char=match.start() + char_start_offset),
                            end=lsp.Position(line=offset, char=match.end())
                        )
                        results.append(lsp.Location(locrange, file_uri))
            return results
        except re.error:
            self._logger.debug("Error building regex pattern: %s", pattern)
            return []
        except Exception as err:
            self._logger.error(err)
            raise ce.DefinitionError("Could not offer definition for symbol '{}': {}".format(symbol, err))

    async def provide_diagnostic(self, document: str) -> list:
        ''' Respond to the textDocument/publishDiagnostics request

        :document: Contents of YARA rule file
        '''
        try:
            if HAS_YARA:
                diagnostics = []
                try:
                    yara.compile(source=document)
                except yara.SyntaxError as error:
                    line_no, msg = helpers.parse_result(str(error))
                    # VSCode is zero-indexed
                    line_no -= 1
                    first_char = helpers.get_first_non_whitespace_index(document.split("\n")[line_no])
                    symbol_range = lsp.Range(
                        start=lsp.Position(line_no, first_char),
                        end=lsp.Position(line_no, self.MAX_LINE)
                    )
                    diagnostics.append(
                        lsp.Diagnostic(
                            locrange=symbol_range,
                            severity=lsp.DiagnosticSeverity.ERROR,
                            message=msg
                        )
                    )
                except yara.WarningError as warning:
                    line_no, msg = helpers.parse_result(str(warning))
                    # VSCode is zero-indexed
                    line_no -= 1
                    first_char = helpers.get_first_non_whitespace_index(document.split("\n")[line_no])
                    symbol_range = lsp.Range(
                        start=lsp.Position(line_no, first_char),
                        end=lsp.Position(line_no, self.MAX_LINE)
                    )
                    diagnostics.append(
                        lsp.Diagnostic(
                            locrange=symbol_range,
                            severity=lsp.DiagnosticSeverity.WARNING,
                            message=msg
                        )
                    )
                return diagnostics
            elif self.diagnostics_warned:
                pass
            else:
                self.diagnostics_warned = True
                raise ce.NoYaraPython("yara-python is not installed. Diagnostics and Compile commands are disabled")
        except Exception as err:
            self._logger.error(err)
            raise ce.DiagnosticError("Could not compile rule: {}".format(err))

    async def provide_highlight(self, params: dict, document: str) -> list:
        ''' Respond to the textDocument/documentHighlight request '''
        try:
            self._logger.warning("provide_highlight() is not implemented")
            results = []
            return results
        except Exception as err:
            self._logger.error(err)
            raise ce.HighlightError("Could not offer code highlighting: {}".format(err))

    async def provide_hover(self, params: dict, document: str) -> list:
        ''' Respond to the textDocument/hover request '''
        try:
            definitions = await self.provide_definition(params, document)
            if len(definitions) > 0:
                # only care about the first definition; although there shouldn't be more
                definition = definitions[0]
                line = document.split("\n")[definition.range.start.line]
                try:
                    words = line.split(" = ")
                    if len(words) > 1:
                        contents = lsp.MarkupContent(lsp.MarkupKind.Plaintext, content=words[1])
                        return lsp.Hover(contents)
                except IndexError as err:
                    self._logger.warning(words)
                    self._logger.warning("IndexError at line %d: '%s'", definition.range.start.line, line)
            return None
        except Exception as err:
            self._logger.error(err)
            raise ce.HoverError("Could not offer definition hover: {}".format(err))

    async def provide_reference(self, params: dict, document: str) -> list:
        '''The references request is sent from the client to the server to resolve
        project-wide references for the symbol denoted by the given text document position

        Returns a (possibly empty) list of symbol Locations
        '''
        results = []
        file_uri = params.get("textDocument", {}).get("uri", None)
        pos = lsp.Position(line=params["position"]["line"], char=params["position"]["character"])
        symbol = helpers.resolve_symbol(document, pos)
        if not symbol:
            return []
        try:
            # gotta match the wildcard variables first to build the correct regex pattern
            # I don't think wildcards are technially supposed to work for rules, but a diagnostic
            # will appear to the user if YARA can't compile it, so I won't worry too much
            wildcard_found = ("*" in symbol)
            if wildcard_found:
                # remove parentheses and replace the YARA wildcard with a Python re equivalent
                symbol = symbol.replace("*", ".*?").strip("()")
            # check to see if the symbol is a variable or a rule name (currently the only valid symbols)
            if symbol[0] in self._varchar:
                # any possible first character matching self._varchar must be treated as a reference
                pattern = "[{}]{}\\b".format("".join(self._varchar), "".join(symbol[1:]))
                rule_range = helpers.get_rule_range(document, pos)
                rule_lines = document.split("\n")[rule_range.start.line:rule_range.end.line+1]
                rel_offset = rule_range.start.line
                char_start_offset = 1
                if wildcard_found:
                    # only search strings section if this is a wildcard variable
                    # figure out the bounds of the strings section
                    strings_start = [idx for idx, line in enumerate(rule_lines) if "strings:" in line][0]
                    strings_end = [idx for idx, line in enumerate(rule_lines) if "condition:" in line][0]
                    rule_lines = rule_lines[strings_start:strings_end]
                    rel_offset += strings_start
            else:
                rel_offset = 0
                pattern = "{}\\b".format(symbol)
                rule_lines = document.split("\n")
                char_start_offset = 0

            for index, line in enumerate(rule_lines):
                for match in re.finditer(pattern, line):
                    if match:
                        # index corresponds to line no. within each rule, not within file
                        offset = rel_offset + index
                        locrange = lsp.Range(
                            start=lsp.Position(line=offset, char=match.start() + char_start_offset),
                            end=lsp.Position(line=offset, char=match.end())
                        )
                        results.append(lsp.Location(locrange, file_uri))
            return results
        except re.error:
            self._logger.debug("Error building regex pattern: %s", pattern)
            return []
        except Exception as err:
            self._logger.error(err)
            raise ce.SymbolReferenceError("Could not find references for '{}': {}".format(symbol, err))

    async def provide_rename(self, params: dict, document: str, file_uri: str) -> list:
        ''' Respond to the textDocument/rename request '''
        results = lsp.WorkspaceEdit(file_uri=file_uri, changes=[])
        try:
            pos = lsp.Position(line=params["position"]["line"], char=params["position"]["character"])
            old_text = helpers.resolve_symbol(document, pos)
            new_text = params.get("newName", None)
            if new_text is None:
                self._logger.warning("No text to rename symbol to. Skipping")
            elif new_text == old_text:
                self._logger.warning("New rename symbol is the same as the old. Skipping")
            elif old_text.endswith("*"):
                self._logger.warning("Cannot rename wildcard symbols. Skipping")
            # let provide_reference() determine symbol or rule
            # and therefore what scope to look into
            refs = await self.provide_reference(params, document)
            for ref in refs:
                # need to add one character to the position so the variable
                # type is not overwritten
                new_range = lsp.Range(
                    lsp.Position(ref.range.start.line, ref.range.start.char+1),
                    lsp.Position(ref.range.end.line, ref.range.end.char)
                )
                results.append(lsp.TextEdit(new_range, new_text))
            if len(results.changes) <= 0:
                self._logger.warning("No symbol references found to rename. Skipping")
        except Exception as err:
            self._logger.error(err)
            raise ce.RenameError("Could not rename symbol: {}".format(err))
        finally:
            # pylint: disable=W0150
            return results
