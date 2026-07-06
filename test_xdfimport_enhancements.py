#!/usr/bin/env python
"""
Test enhanced xdfimport functions.
Run: python test_xdfimport_enhancements.py
"""

import sys
import re
import math

def evaluate_math(equation: str, raw_value: float,
                  axis_context: dict = None,
                  linked_vars: dict = None) -> tuple:
    """Test version of evaluate_math (copied for standalone testing)."""
    if axis_context is None:
        axis_context = {}

    equation = re.sub(r'&#\d+;', '', equation)
    equation = equation.strip()

    if not equation or equation.lower() in ('(null)', 'null', ''):
        return float(raw_value), ""

    if equation.strip().upper() == 'X':
        return float(raw_value), ""

    try:
        equation_fixed = equation.strip()

        if equation_fixed.startswith(('*', '/')):
            equation_fixed = 'X' + equation_fixed

        equation_fixed = re.sub(r'\bX\d+\b', str(raw_value), equation_fixed, flags=re.IGNORECASE)

        tp_if_match = re.match(
            r'^\s*if\s*\(\s*(.+?)\s*;\s*(.+?)\s*;\s*(.+?)\s*\)\s*$',
            equation_fixed, re.IGNORECASE
        )
        if tp_if_match:
            cond_part = tp_if_match.group(1).strip()
            true_part = tp_if_match.group(2).strip()
            false_part = tp_if_match.group(3).strip()
            equation_fixed = f"({true_part}) if ({cond_part}) else ({false_part})"

        if '>>' in equation_fixed or '<<' in equation_fixed:
            equation_fixed = re.sub(r'\bY\b', 'int(Y)', equation_fixed)
            equation_fixed = re.sub(r'\bX\b', 'int(X)', equation_fixed)
            equation_fixed = re.sub(r'\bA\b', 'int(A)', equation_fixed)
            equation_fixed = re.sub(r'\bB\b', 'int(B)', equation_fixed)
            equation_fixed = re.sub(r'\bE\b', 'int(E)', equation_fixed)
            equation_fixed = re.sub(r'int\(int\(([^)]+)\)\)', r'int(\1)', equation_fixed)

        namespace = {
            'X': raw_value, 'x': raw_value,
            'A': axis_context.get('row_index', 0),
            'a': axis_context.get('row_index', 0),
            'B': max(axis_context.get('col_index', 1), 1),
            'b': max(axis_context.get('col_index', 1), 1),
            'E': axis_context.get('element_count', 1),
            'e': axis_context.get('element_count', 1),
            'Y': axis_context.get('y_axis_value', 0),
            'y': axis_context.get('y_axis_value', 0),
            'Z': axis_context.get('x_axis_value', 0),
            'z': axis_context.get('x_axis_value', 0),
            'exp': math.exp, 'log': math.log, 'log10': math.log10,
            'sqrt': math.sqrt, 'pow': math.pow, 'abs': abs,
            'int': int, 'float': float,
            'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
            'PI': math.pi, 'pi': math.pi,
            '__builtins__': {}
        }

        if linked_vars:
            for var_name, var_val in linked_vars.items():
                if var_val is not None:
                    namespace[var_name] = var_val
                    namespace[var_name.lower()] = var_val

        result = eval(equation_fixed, namespace)

        if math.isinf(result):
            return None, f"Division by zero (result=inf) in '{equation}'"
        if math.isnan(result):
            return None, f"Invalid math operation (result=NaN) in '{equation}'"

        return float(result), ""

    except ZeroDivisionError:
        return None, f"Division by zero in '{equation}' (X={raw_value})"
    except Exception as e:
        return None, f"Math evaluation failed for '{equation}' (X={raw_value}): {str(e)}"


def run_tests():
    """Run comprehensive functional tests."""
    print("Testing enhanced xdfimport math evaluation...")
    print()

    tests_passed = 0
    tests_total = 0

    # Test 1: Ternary if/then/else
    tests_total += 1
    result, err = evaluate_math('if(X > 50 ; 100 ; X*2)', 75)
    if result == 100 and err == '':
        print('[OK] Test 1: Ternary if/then/else -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 1: FAILED -- result={result}, err={err}')

    # Test 2: Bitshift
    tests_total += 1
    result, err = evaluate_math('X >> 2', 64)
    if result == 16 and err == '':
        print('[OK] Test 2: Bitshift support -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 2: FAILED -- result={result}, err={err}')

    # Test 3: Math functions (sqrt)
    tests_total += 1
    result, err = evaluate_math('sqrt(16)', 0)
    if result == 4 and err == '':
        print('[OK] Test 3: Math functions (sqrt) -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 3: FAILED -- result={result}, err={err}')

    # Test 4: Bitshift with int-wrapping on float
    tests_total += 1
    result, err = evaluate_math('X >> 3', 64.5)
    if result == 8 and err == '':
        print('[OK] Test 4: Bitshift auto-int-wrapping on float -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 4: FAILED -- result={result}, err={err}')

    # Test 5: Linked variable resolution
    tests_total += 1
    result, err = evaluate_math('X / B', 64, linked_vars={'B': 4})
    if result == 16 and err == '':
        print('[OK] Test 5: Linked variable resolution -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 5: FAILED -- result={result}, err={err}')

    # Test 6: Axis context (row_index, col_index)
    tests_total += 1
    result, err = evaluate_math('X + A', 10, axis_context={'row_index': 5})
    if result == 15 and err == '':
        print('[OK] Test 6: Axis context (row_index, col_index) -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 6: FAILED -- result={result}, err={err}')

    # Test 7: Log function
    tests_total += 1
    result, err = evaluate_math('log(X)', 2.718281828)
    if result is not None and 0.9 < result < 1.1 and err == '':
        print('[OK] Test 7: Log function -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 7: FAILED -- result={result}, err={err}')

    # Test 8: Division by zero protection
    tests_total += 1
    result, err = evaluate_math('1/X', 0)
    if result is None and 'Division by zero' in err:
        print('[OK] Test 8: Division by zero detection -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 8: FAILED -- result={result}, err={err}')

    # Test 9: Complex ternary condition
    tests_total += 1
    result, err = evaluate_math('if(X > 100 ; X*2 ; X/2)', 150)
    if result == 300 and err == '':
        print('[OK] Test 9: Complex ternary condition -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 9: FAILED -- result={result}, err={err}')

    # Test 10: Trigonometric functions
    tests_total += 1
    result, err = evaluate_math('sin(X)', 0)
    if result is not None and abs(result) < 0.01 and err == '':
        print('[OK] Test 10: Trigonometric functions -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 10: FAILED -- result={result}, err={err}')

    # Test 11: Exp function
    tests_total += 1
    result, err = evaluate_math('exp(1)', 0)
    if result is not None and 2.7 < result < 2.8 and err == '':
        print('[OK] Test 11: Exp function -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 11: FAILED -- result={result}, err={err}')

    # Test 12: Linear factor extraction (simple X)
    tests_total += 1
    result, err = evaluate_math('X', 42)
    if result == 42 and err == '':
        print('[OK] Test 12: Passthrough X -- PASSED')
        tests_passed += 1
    else:
        print(f'[FAIL] Test 12: FAILED -- result={result}, err={err}')

    print()
    print('=' * 60)
    print(f'Results: {tests_passed}/{tests_total} tests PASSED')
    print('=' * 60)

    if tests_passed == tests_total:
        print()
        print('[OK] ALL TESTS PASSED')
        return 0
    else:
        print()
        print(f'[FAIL] {tests_total - tests_passed} TEST(S) FAILED')
        return 1


if __name__ == '__main__':
    sys.exit(run_tests())
