/* gilneeder.c — experiment: an extension module that does NOT declare Py_MOD_GIL_NOT_USED.
 * Dropped into a free-threaded runtime, importing it re-enables the GIL, which
 * build_runtime.py's gate must refuse. Used by gil_gate_negative.sh only. */
#include <Python.h>

static struct PyModuleDef gilneeder = {PyModuleDef_HEAD_INIT, "gilneeder", NULL, 0, NULL};

PyMODINIT_FUNC PyInit_gilneeder(void) { return PyModuleDef_Init(&gilneeder); }
