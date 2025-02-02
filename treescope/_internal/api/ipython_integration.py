# Copyright 2024 The Treescope Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Support for using treescope in IPython."""

import contextlib
from typing import Any

from treescope import context
from treescope import figures
from treescope import lowering
from treescope import rendering_parts
from treescope._internal import object_inspection
from treescope._internal.api import array_autovisualizer
from treescope._internal.api import autovisualize
from treescope._internal.api import default_renderer

# pylint: disable=g-import-not-at-top
try:
  import IPython
except ImportError:
  IPython = None
else:
  import IPython.core.formatters
  import IPython.display
# pylint: enable=g-import-not-at-top


def display(
    value: Any,
    ignore_exceptions: bool = False,
    roundtrip_mode: bool = False,
):
  """Displays a value as an interactively foldable object.

  Uses the default renderer.

  Args:
    value: Value to fold.
    ignore_exceptions: Whether to catch errors during rendering of subtrees and
      show a fallback for those subtrees.
    roundtrip_mode: Whether to start in roundtrip mode.

  Raises:
    RuntimeError: If IPython is not available.
  """
  if IPython is None:
    raise RuntimeError("Cannot use `display` outside of IPython.")
  IPython.display.display(
      IPython.display.HTML(
          default_renderer.render_to_html(
              value,
              ignore_exceptions=ignore_exceptions,
              roundtrip_mode=roundtrip_mode,
              compressed=True,
          ),
      )
  )


def show(*args, wrap: bool = False, space_separated: bool = True):
  """Shows a list of objects inline, like python print, but with rich display.

  Args:
    *args: Values to show. Strings show as themselves, like python's print.
      Anything renderable by Treescope will show as it's treescope
      representation. Anything with a rich IPython representation will show as
      its IPython representation.
    wrap: Whether to wrap at the end of the line.
    space_separated: Whether to add single spaces between objects.

  Raises:
    RuntimeError: If IPython is not available.
  """
  if IPython is None:
    raise RuntimeError("Cannot use `show` outside of IPython.")
  if space_separated and args:
    separated_args = []
    for arg in args:
      separated_args.append(arg)
      separated_args.append(" ")
    args = separated_args[:-1]
  IPython.display.display(figures.inline(*args, wrap=wrap))


def register_as_default(
    streaming: bool = True,
    compress_html: bool = True,
):
  """Registers treescope as the default IPython renderer.

  This tells IPython to use treescope as a renderer for any object
  that doesn't have a specific renderer registered with IPython directly.

  Treescope will be configured to produce an HTML representation of most
  objects that do not have their own custom renderers. It will also be
  configured to produce summaries of jax.Array in text mode. Note that due to
  the way that IPython's text prettyprinter works, we can't easily set it up
  as a fallback renderer in text mode because IPython will prefer to use
  ordinary `repr` if it exists.

  Note that this hooks into every use of ``IPython.display.display(...)``. To
  avoid messing up ordinary display objects, if the object has a _repr_html_
  method already, we defer to that. (But if it's a structure containing display
  objects, we still use treescope as normal.)

  If the root object being rendered defines the special method
  `__treescope_root_repr__`, that method will be assumed to take no arguments
  and return a representation of the root object in Treescope's intermediate
  representation. This can be used to fully customize the rendering of a
  particular type of object. (Most types should instead define
  `__treescope_repr__`, which allows the rendering to be customized at any level
  of the tree, not just the root.)

  Args:
    streaming: Whether to render in streaming mode, which immediately displays
      the structure of the output while computing more expensive leaf
      renderings. This is useful in interactive contexts, but can mess with
      other users of IPython's formatting because the final rendered HTML is
      empty.
    compress_html: Whether to zlib-compress (i.e. zip) treescope renderings to
      reduce their size when transmitted to the browser or saved into a
      notebook.

  Raises:
    RuntimeError: If IPython is not available.
  """
  if IPython is None:
    raise RuntimeError("Cannot use `register_as_default` outside of IPython.")

  ipython_display = IPython.display

  def _render_for_ipython(value):
    repr_html_method = object_inspection.safely_get_real_method(
        value, "_repr_html_"
    )
    if repr_html_method:
      return repr_html_method()  # pylint: disable=protected-access
    elif isinstance(value, ipython_display.DisplayObject) or (
        object_inspection.safely_get_real_method(value, "_repr_pretty_")
        and not (
            object_inspection.safely_get_real_method(
                value, "__treescope_repr__"
            )
            or object_inspection.safely_get_real_method(
                value, "__treescope_root_repr__"
            )
        )
    ):
      # Don't render this to HTML.
      return None
    else:
      with contextlib.ExitStack() as stack:
        if streaming:
          # Render using Treescope. However, since the display_formatter is used
          # in an interactive context, we can defer rendering of leaves that
          # support deferral and splice them in one at a time.
          deferreds = stack.enter_context(
              lowering.collecting_deferred_renderings()
          )
        else:
          deferreds = None

        root_repr_method = object_inspection.safely_get_real_method(
            value, "__treescope_root_repr__"
        )
        if root_repr_method:
          foldable_ir = root_repr_method()
        else:
          foldable_ir = rendering_parts.build_full_line_with_annotations(
              default_renderer.build_foldable_representation(
                  value, ignore_exceptions=True
              )
          )
        if streaming:
          output_stealer = lowering.display_streaming_as_root(
              foldable_ir,
              deferreds,
              roundtrip=False,
              compressed=compress_html,
              stealable=True,
          )
          # Executing the above call will have already displayed the output,
          # but it may be in the wrong place (e.g. it may appear before the
          # actual "Out" marker in JupyterLab). By returning `output_stealer`
          # as the rendering of the object, we can ensure that the output is
          # moved to the right place.
          return output_stealer
        else:
          assert deferreds is None
          return lowering.render_to_html_as_root(
              foldable_ir,
              roundtrip=False,
              compressed=compress_html,
          )

  display_formatter = IPython.get_ipython().display_formatter
  cur_html_formatter = display_formatter.formatters["text/html"]
  cur_html_formatter.for_type(object, _render_for_ipython)

  def _render_as_text_oneline(value, p, cycle):
    del cycle
    with default_renderer.using_expansion_strategy(max_height=None):
      rendering = default_renderer.render_to_text(value, ignore_exceptions=True)
    for i, line in enumerate(rendering.split("\n")):
      if i:
        p.break_()
      p.text(line)

  # Override the text formatter to render jax.Array without copying the entire
  # array.
  cur_text_formatter = display_formatter.formatters["text/plain"]
  cur_text_formatter.for_type_by_name(
      "jaxlib.xla_extension", "ArrayImpl", _render_as_text_oneline
  )

  # Make sure the HTML formatter runs first, so streaming outputs work
  # correctly.
  old_formatters = display_formatter.formatters
  display_formatter.formatters = {}
  display_formatter.formatters["text/html"] = cur_html_formatter
  for k, v in old_formatters.items():
    if k != "text/html":
      display_formatter.formatters[k] = v

  try:
    from google.colab import _reprs  # pylint: disable=g-import-not-at-top  # pytype: disable=import-error

    _reprs.disable_string_repr()
    try:
      _reprs.disable_ndarray_repr()
    except KeyError:
      pass
    try:
      _reprs.disable_function_repr()
    except KeyError:
      pass
  except ImportError:
    pass


default_magic_autovisualizer: context.ContextualValue[
    autovisualize.Autovisualizer
] = context.ContextualValue(
    module=__name__,
    qualname="default_magic_autovisualizer",
    initial_value=array_autovisualizer.ArrayAutovisualizer(),
)


if IPython is not None:

  @IPython.core.magic.magics_class
  class AutovisualizerMagic(IPython.core.magic.Magics):
    """Magics class for enabling automatic visualization."""

    @IPython.core.magic.cell_magic
    def autovisualize(self, line, cell):
      """``%%autovisualize`` cell magic: enables autovisualization in a cell.

      The ``%%autovisualize`` magic is syntactic sugar for running a cell with
      automatic visualization turned on. To use the default autovisualizer,
      you can annotate your cell with ::

        %%autovisualize

        # ... contents of your cell ...
        result = ...
        result

      which expands to something like ::

        with treescope.active_autovisualizer.set_scoped(
            treescope.default_magic_autovisualizer.get()
        ):
          # ... contents of your cell ...
          result = ...
          IPython.display.display(result)


      You can also pass an explicit autovisualizer function/object::

        %%autovisualize my_autovisualizer

        # ... contents of your cell ...
        result = ...
        result

      which expands to something like ::

        with treescope.active_autovisualizer.set_scoped(my_autovisualizer):
          # ... contents of your cell ...
          result = ...
          IPython.display.display(result)

      Args:
        line: Contents of the line where ``%%autovisualize`` is. Should either
          be empty or should be (a Python expression for) an autovisualizer
          object to use.
        cell: Contents of the rest of the cell. Will be run inside the
          autovisualization scope.
      """
      if line.strip():
        # Evaluate the line as Python code.
        autovisualizer = self.shell.ev(line)
      else:
        # Retrieve the default autovisualizer.
        autovisualizer = default_magic_autovisualizer.get()
      if autovisualizer is None:
        autovisualizer = lambda value, path: None
      with autovisualize.active_autovisualizer.set_scoped(autovisualizer):
        self.shell.run_cell(cell)

  @IPython.core.magic.magics_class
  class ContextManagerMagic(IPython.core.magic.Magics):
    """Magics class for using ``%%with`` to run a cell under a context manager."""

    @IPython.core.magic.cell_magic("with")
    def with_(self, line, cell):
      """``%%with`` cell magic: runs under a context manager.

      The ``%%with`` magic is syntactic sugar for running a cell under a context
      manager. It can be used to easily set Treescope's context variables in a
      cell without having to explicitly display the final output. In other
      words, ::

        %%with some_var.set_scoped(Foo)
        result = ...
        result

      expands to something like ::

        with some_var.set_scoped(Foo):
          result = ...
          IPython.display.display(result)

      Args:
        line: Contents of the line where ``%%with`` is. Should be a Python
          expression for a context manager.
        cell: Contents of the rest of the cell. Will be run inside the context
          manager scope.
      """
      ctxmgr = self.shell.ev(line)
      with ctxmgr:
        self.shell.run_cell(cell)

else:
  AutovisualizerMagic = None  # pylint: disable=invalid-name
  ContextManagerMagic = None  # pylint: disable=invalid-name


def register_autovisualize_magic():
  """Registers the ``%%autovisualize`` magic.

  This makes it possible to use ``%%autovisualize`` at the top of a cell to
  enable automatic visualization for that cell's outputs.

  Raises:
    RuntimeError: If IPython is not available.
  """
  if IPython is None:
    raise RuntimeError(
        "Cannot use `register_autovisualize_magic` outside of IPython."
    )
  IPython.get_ipython().register_magics(AutovisualizerMagic)


def register_context_manager_magic():
  """Registers the ``%%with`` magic.

  This makes it possible to use ``%%with`` at the top of a cell to enable
  automatic visualization for that cell's outputs.

  Raises:
    RuntimeError: If IPython is not available.
  """
  if IPython is None:
    raise RuntimeError(
        "Cannot use `register_context_manager_magic` outside of IPython."
    )
  IPython.get_ipython().register_magics(ContextManagerMagic)


def basic_interactive_setup(autovisualize_arrays: bool = True):
  """Sets up IPython for interactive use with Treescope.

  This is a helper function that runs various setup steps:

    * Configures Treescope as the default IPython renderer.
    * Turns on interactive mode for Treescope's context managers.
    * Registers the `%%autovisualize` magic.
    * Registers the `%%with` magic.
    * If `autovisualize_arrays` is True, configures Treescope to automatically
      visualize arrays.

  Args:
    autovisualize_arrays: Whether to automatically visualize arrays.
  """
  register_as_default()
  register_autovisualize_magic()
  register_context_manager_magic()

  if autovisualize_arrays:
    autovisualize.active_autovisualizer.set_globally(
        array_autovisualizer.ArrayAutovisualizer()
    )
