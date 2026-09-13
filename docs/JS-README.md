# JavaScript Interview Preparation

A sequential guide covering **54 core concepts** and **14 polyfills**. Each topic has a plain-language explanation, interview-ready code, and the “why it matters” takeaway.

Read top to bottom. Later topics reuse earlier ones (scope → closures → event loop → promises).

---

## How to use this file

1. Read the explanation first. Do not jump to the code.
2. Type the example yourself. Change one line and predict the output.
3. For polyfills, implement from memory, then compare.
4. Say the answer out loud as if you are in a 30-minute interview.

---

## Contents

**Part A — Concepts**

1. [Scope (global, function, block)](#1-scope-in-javascript-global-function-block)
2. [Scope chaining](#2-scope-chaining)
3. [Primitive and non-primitive](#3-primitive-and-non-primitive-types)
4. [`var`, `let`, `const`](#4-var-let-and-const)
5. [Temporal Dead Zone](#5-temporal-dead-zone-tdz)
6. [Hoisting](#6-hoisting)
7. [Prototypes](#7-prototypes-in-javascript)
8. [Prototype object](#8-prototype-object)
9. [Prototype chaining](#9-prototype-chaining)
10. [Closures](#10-closures)
11. [Pass by reference vs value](#11-pass-by-reference-vs-pass-by-value)
12. [Currying](#12-currying-in-javascript)
13. [Infinite currying](#13-infinite-currying-problem)
14. [Memoization](#14-memoization-in-javascript)
15. [Rest parameter](#15-rest-parameter)
16. [Spread operators](#16-spread-operators)
17. [Ways to create an object](#17-how-many-ways-can-you-create-an-object-in-javascript)
18. [Generator functions](#18-generator-functions)
19. [Single-threaded JS](#19-javascript-is-a-single-threaded-language)
20. [Callbacks](#20-callbacks--why-we-need-them)
21. [Event loop](#21-event-loops)
22. [Callback / macrotask queue](#22-callback-queue-macrotask--task-queue)
23. [Microtask queue](#23-microtask-queue)
24. [Promises](#24-promises)
25. [Event propagation](#25-event-propagation)
26. [Event bubbling](#26-event-bubbling)
27. [Event capturing](#27-event-capturing)
28. [`stopPropagation`](#28-event-stoppropagation)
29. [Event delegation](#29-event-delegation)
30. [Coercion vs conversion](#30-type-coercion-vs-conversion)
31. [Throttle](#31-throttle)
32. [Debounce](#32-debounce)
33. [Parse and compile](#33-how-javascript-parses-and-compiles-your-code-step-by-step)
34. [Infinite microtasks](#34-infinite-microtasks--how-do-you-handle-them)
35. [First-class functions](#35-first-class-functions)
36. [IIFE](#36-immediately-invoked-function-expressions-iife)
37. [`call`, `apply`, `bind`](#37-call-apply-and-bind--why-we-use-them)
38. [MapLimit](#38-maplimit)
39. [`async` / `await`](#39-async-and-await)
40. [`then` / `catch`](#40-then-and-catch)
41. [Variable shadowing](#41-variable-shadowing)
42. [`static` in a class](#42-what-does-static-mean-in-a-javascript-class)
43. [`undefined` vs not-defined vs `null`](#43-undefined-vs-not-defined-vs-null)
44. [Higher-order functions](#44-higher-order-functions)
45. [Callback hell](#45-callback-hell)
46. [`this`](#46-this-in-javascript)
47. [Function types](#47-function-declaration-expression-anonymous-functions-arrow-functions)
48. [`async` vs `defer`](#48-async-vs-defer-on-script)
49. [Execution context](#49-execution-context)
50. [Call stack](#50-call-stack)
51. [Garbage collection](#51-garbage-collection)
52. [`===` vs `==`](#52-equality--vs-)
53. [Strict mode](#53-strict-mode)
54. [Lexical environment](#54-lexical-environment)

**Part B — Polyfills**

1. [`map`, `reduce`, `filter`, `forEach`, `find`](#polyfill-1--map-reduce-filter-foreach-find)
2. [`call`, `apply`, `bind`](#polyfill-2--call-apply-bind)
3. [Promise helpers](#polyfill-3--promise-all-any-allsettled-race)
4. [mapLimit](#polyfill-4--maplimit)
5. [Debounce](#polyfill-5--debounce)
6. [Throttle](#polyfill-6--throttle)
7. [Event emitter](#polyfill-7--event-emitter)
8. [`setInterval`](#polyfill-8--setinterval-polyfill)
9. [Parallel limit](#polyfill-9--parallel-limit-function)
10. [Deep vs shallow copy](#polyfill-10--deep-vs-shallow-copy)
11. [Flatten nested object](#polyfill-11--flatten-a-deeply-nested-object)
12. [Memoization / cache](#polyfill-12--memoization--caching)
13. [`Promise.finally`](#polyfill-13--promiseprototypefinally)
14. [Retry](#polyfill-14--retry)

---

# Part A — Concepts (1–54)

---

## 1. Scope in JavaScript (Global, Function, Block)

**Scope** is the region of code where a variable can be accessed.

JavaScript has three main scopes:

| Scope | Created by | Visible |
|---|---|---|
| **Global** | Top-level `var` / `let` / `const`, or assignment without declaration (non-strict) | Entire program |
| **Function** | A function body | Only inside that function |
| **Block** | `{ }` with `let` / `const` | Only inside that block |

```javascript
var globalVar = "I am global";

function greet() {
  var functionVar = "I am function-scoped";

  if (true) {
    let blockVar = "I am block-scoped";
    const alsoBlock = "me too";
    var stillFunction = "var ignores the if-block";

    console.log(blockVar);      // works
    console.log(functionVar);   // works
    console.log(globalVar);     // works
  }

  console.log(stillFunction);   // works — var is function scoped
  // console.log(blockVar);     // ReferenceError
}

greet();
console.log(globalVar);         // works
// console.log(functionVar);    // ReferenceError
```

**Interview takeaway**

- `var` → function scope (or global if declared outside any function).
- `let` / `const` → block scope.
- A block is any `{ }`: `if`, `for`, `while`, `try`, or a standalone pair of braces.
- In browsers, a global `var` also becomes a property of `window`. `let` / `const` do not.

```javascript
var a = 1;
let b = 2;
console.log(window.a); // 1
console.log(window.b); // undefined
```

---

## 2. Scope chaining

When JavaScript looks up a variable, it does **not** search the whole file. It walks **outward** from the current scope until it finds the name or reaches the global scope.

That walk is **scope chaining**. It is based on **where the function was written** (lexical scope), not where it was called.

```javascript
const name = "global";

function outer() {
  const name = "outer";

  function inner() {
    const name = "inner";
    console.log(name); // "inner" — found here, stop looking
  }

  function middle() {
    console.log(name); // "outer" — not in middle, look in outer
  }

  inner();
  middle();
}

outer();
console.log(name); // "global"
```

If a name is missing in every scope, you get `ReferenceError` (“not defined”).

```javascript
function demo() {
  console.log(missing); // ReferenceError: missing is not defined
}
```

**Interview takeaway**

- Lookup order: current scope → parent → … → global.
- First match wins. That is why inner variables can hide outer ones (shadowing — Q41).
- Calling `inner()` from another file does **not** change which `name` it sees. The chain was fixed at write time.

---

## 3. Primitive and non-primitive types

JavaScript types fall into two groups.

### Primitives (copied by value)

`string`, `number`, `bigint`, `boolean`, `undefined`, `null`, `symbol`

```javascript
let a = 10;
let b = a;
b = 20;
console.log(a); // 10 — a was not changed
```

Primitives are **immutable**. `"hi".toUpperCase()` returns a new string; it does not change `"hi"`.

### Non-primitives / reference types

`object`, `array`, `function`, `date`, `map`, `set`, …

```javascript
let obj1 = { city: "Pune" };
let obj2 = obj1;          // both point to the same object
obj2.city = "Mumbai";
console.log(obj1.city);   // "Mumbai"
```

`typeof` quirks interviewers love:

```javascript
typeof 1          // "number"
typeof "hi"       // "string"
typeof true       // "boolean"
typeof undefined  // "undefined"
typeof null       // "object"  ← historical bug, keep it
typeof Symbol()   // "symbol"
typeof 10n        // "bigint"
typeof {}         // "object"
typeof []         // "object"
typeof function(){} // "function"
```

Use `Array.isArray([])` to detect arrays.

**Interview takeaway**

- Primitive → copy the value.
- Object → copy the reference (the pointer).
- `null` is a primitive even though `typeof null === "object"`.

---

## 4. `var`, `let`, and `const`

| Feature | `var` | `let` | `const` |
|---|---|---|---|
| Scope | Function | Block | Block |
| Hoisted | Yes, initialized as `undefined` | Yes, but in TDZ | Yes, but in TDZ |
| Re-declare in same scope | Allowed | SyntaxError | SyntaxError |
| Re-assign | Allowed | Allowed | Not allowed |
| Global object property | Yes (`window.x`) | No | No |

```javascript
function demo() {
  console.log(a); // undefined — var is hoisted
  // console.log(b); // ReferenceError — TDZ
  // console.log(c); // ReferenceError — TDZ

  var a = 1;
  let b = 2;
  const c = 3;

  var a = 10;     // OK
  // let b = 20;  // SyntaxError
  // c = 30;      // TypeError
}

const user = { name: "Kalpesh" };
user.name = "Pawar"; // OK — the binding is constant, not the object
// user = {};        // TypeError
```

**When to use what**

- Default to `const`.
- Use `let` when the value must change (counters, reassignment).
- Avoid `var` in new code. Know it for interviews and old codebases.

---

## 5. Temporal Dead Zone (TDZ)

The **TDZ** is the time between entering a scope and the line where `let` / `const` is initialized.

The variable **exists** (it was hoisted) but you **cannot read or write** it yet.

```javascript
{
  // TDZ for x starts here
  // console.log(x); // ReferenceError
  let x = 5;         // TDZ ends
  console.log(x);    // 5
}
```

`typeof` is also unsafe in the TDZ:

```javascript
console.log(typeof notDeclared); // "undefined" — name does not exist
console.log(typeof y);           // ReferenceError if y is `let` below
let y = 1;
```

`const` must be initialized on the same line. That initialization is what ends the TDZ.

```javascript
const score = 100; // required
// const score;    // SyntaxError
```

**Interview takeaway**

- Hoisting and TDZ are not opposites. `let` / `const` **are** hoisted; TDZ just blocks access until the declaration runs.
- `var` has no TDZ. It is `undefined` until assignment.

---

## 6. Hoisting

**Hoisting** means declarations are processed before the body runs, so they appear to “move to the top” of their scope.

What actually happens: during **creation** of an execution context (Q49), the engine registers bindings.

```javascript
console.log(x); // undefined
var x = 10;

sayHi();        // works
function sayHi() {
  console.log("hi");
}

// sayBye();    // TypeError: sayBye is not a function
var sayBye = function () {
  console.log("bye");
};
```

Rules:

1. **Function declarations** — fully hoisted (name + body). You can call them above the definition.
2. **`var`** — hoisted, value `undefined` until assignment.
3. **`let` / `const`** — hoisted into TDZ.
4. **Function expressions / arrows** — only the variable is hoisted (`var` → `undefined`, `let` → TDZ). The function value is not.

```javascript
foo(); // TypeError, not ReferenceError
var foo = () => {};
```

Class declarations behave like `let` (TDZ):

```javascript
// new User(); // ReferenceError
class User {}
```

**Interview takeaway**

Never rely on hoisting in production. Explain it as “creation phase registers names before the code runs.”

---

## 7. Prototypes in JavaScript

Almost every object has a hidden link to another object: its **prototype**.

When you read `obj.key` and `key` is not on `obj`, JavaScript looks at the prototype, then that object’s prototype, and so on (prototype chaining — Q9).

```javascript
const animal = {
  eat() {
    return "eating";
  },
};

const dog = Object.create(animal);
dog.bark = function () {
  return "woof";
};

console.log(dog.bark()); // own method
console.log(dog.eat());  // found on prototype
console.log(dog.hasOwnProperty("eat")); // false
console.log(dog.hasOwnProperty("bark")); // true
```

Functions used as constructors get a `.prototype` property. Instances created with `new` get that object as their prototype.

```javascript
function Person(name) {
  this.name = name;
}

Person.prototype.greet = function () {
  return `Hello, I am ${this.name}`;
};

const p = new Person("Asha");
console.log(p.greet());
console.log(Object.getPrototypeOf(p) === Person.prototype); // true
```

**Interview takeaway**

Prototypes are how JavaScript shares methods without copying them onto every instance. This is the old (and still important) inheritance model. `class` syntax is sugar over the same mechanism.

---

## 8. Prototype object

Two different things share the word “prototype”. Interviewers mix them on purpose.

### A. `[[Prototype]]` (internal)

The object this object inherits from. You read it with:

```javascript
Object.getPrototypeOf(obj);
obj.__proto__; // works, but avoid in new code
```

### B. `Fn.prototype` (the prototype object)

A regular object attached to a **function**. `new Fn()` sets the instance’s `[[Prototype]]` to `Fn.prototype`.

```javascript
function Car(brand) {
  this.brand = brand;
}

console.log(typeof Car.prototype); // "object"
Car.prototype.drive = function () {
  return `${this.brand} is moving`;
};

const c = new Car("Tata");
console.log(c.drive());

// The instance does NOT have a `.prototype` property
console.log(c.prototype); // undefined
console.log(c.__proto__ === Car.prototype); // true
```

Default contents of `Fn.prototype`:

```javascript
function Foo() {}
console.log(Foo.prototype.constructor === Foo); // true
```

`Object.prototype` is the top of most chains. It has `toString`, `hasOwnProperty`, `valueOf`, etc.

```javascript
const o = {};
console.log(Object.getPrototypeOf(o) === Object.prototype); // true
console.log(Object.getPrototypeOf(Object.prototype));       // null
```

**Interview takeaway**

- Instances have `[[Prototype]]`.
- Constructor functions have `.prototype`.
- `obj.__proto__ === Constructor.prototype`.

---

## 9. Prototype chaining

The **prototype chain** is the linked list of objects used for property lookup.

```
dog → animal → Object.prototype → null
```

```javascript
const grand = { level: "grand" };
const parent = Object.create(grand);
parent.level = "parent";

const child = Object.create(parent);
// child.level is not set

console.log(child.level); // "parent" — first match on the chain
console.log(child.toString()); // from Object.prototype
```

Writing a property does **not** walk the chain (unless it is an accessor). It creates an **own** property.

```javascript
child.level = "child";
console.log(child.level);  // "child"
console.log(parent.level); // "parent" — unchanged
```

`for…in` walks the chain. `Object.keys` / `hasOwnProperty` do not.

```javascript
for (const key in child) {
  console.log(key); // own + inherited enumerable keys
}
```

Break the chain with `Object.create(null)` — a “pure dictionary” with no `toString`.

**Interview takeaway**

Read walks the chain. Write (usually) does not. The chain ends at `null`.

---

## 10. Closures

A **closure** is a function that remembers variables from the scope where it was created, even after that outer function has returned.

```javascript
function makeCounter() {
  let count = 0; // private

  return function increment() {
    count += 1;
    return count;
  };
}

const c1 = makeCounter();
const c2 = makeCounter();

console.log(c1()); // 1
console.log(c1()); // 2
console.log(c2()); // 1 — separate closed-over `count`
```

Why this works: `increment` keeps a hidden reference to `makeCounter`’s **lexical environment**. The local `count` is not garbage-collected while `c1` exists.

Classic interview bug — all functions share one `i` with `var`:

```javascript
for (var i = 0; i < 3; i++) {
  setTimeout(() => console.log(i), 0);
}
// 3, 3, 3

for (let i = 0; i < 3; i++) {
  setTimeout(() => console.log(i), 0);
}
// 0, 1, 2  — each iteration has its own block binding
```

Use cases: data privacy, partial application, memoization, event handlers, React hooks (same idea).

**Interview takeaway**

Closure = function + the lexical environment it was born in. Not “a function inside a function” alone — the inner function must **use** outer variables and **outlive** that scope.

---

## 11. Pass by reference vs pass by value

JavaScript always passes **by value**. The catch: for objects the value being copied is the **reference**.

### Primitives — copy of the value

```javascript
function bump(n) {
  n = n + 1;
  return n;
}

let score = 10;
bump(score);
console.log(score); // 10
```

### Objects — copy of the pointer

```javascript
function rename(user) {
  user.name = "New"; // mutates the same object
}

const u = { name: "Old" };
rename(u);
console.log(u.name); // "New"
```

Rebinding the parameter does **not** change the caller’s variable:

```javascript
function replace(user) {
  user = { name: "Replaced" }; // new local pointer
}

const person = { name: "Original" };
replace(person);
console.log(person.name); // "Original"
```

**Interview takeaway**

Say: “JavaScript is pass-by-value. For objects the value is a reference, so mutations are visible to the caller, but reassignment is not.” Do not say “objects are pass-by-reference” without that nuance — strict pass-by-reference would let `replace` change `person`.

---

## 12. Currying in JavaScript

**Currying** turns a function of N arguments into a chain of N unary functions.

```javascript
function add(a, b, c) {
  return a + b + c;
}

function curryAdd(a) {
  return function (b) {
    return function (c) {
      return a + b + c;
    };
  };
}

console.log(add(1, 2, 3));       // 6
console.log(curryAdd(1)(2)(3));  // 6
```

Generic curry (fixed arity):

```javascript
function curry(fn) {
  return function curried(...args) {
    if (args.length >= fn.length) {
      return fn.apply(this, args);
    }
    return function (...next) {
      return curried.apply(this, args.concat(next));
    };
  };
}

function volume(l, w, h) {
  return l * w * h;
}

const curriedVolume = curry(volume);
console.log(curriedVolume(2)(3)(4));
console.log(curriedVolume(2, 3)(4));
```

Why interviewers like it: closures, partial application, reusable configured functions (`const inr = formatCurrency("INR")`).

**Interview takeaway**

Currying is not the same as partial application, but they are cousins. Curry = one argument at a time (classically). Partial application = pre-fill some arguments and return a function that takes the rest.

---

## 13. Infinite currying problem

**Infinite currying** means you can keep calling `fn(1)(2)(3)(4)…` until you stop (usually with `()` or by using the result as a number).

```javascript
function add(a) {
  const next = function (b) {
    if (b === undefined) {
      return a;
    }
    return add(a + b);
  };

  next.valueOf = function () {
    return a;
  };

  return next;
}

console.log(add(1)(2)(3)());           // 6
console.log(add(1)(2)(3)(4)());        // 10
console.log(Number(add(1)(2)(3)));     // 6 via valueOf
```

Another common stop condition: next argument is not a number.

```javascript
function sum(a) {
  return function (b) {
    if (typeof b !== "number") {
      return a;
    }
    return sum(a + b);
  };
}

console.log(sum(1)(2)(3)("stop")); // 6
```

**Interview takeaway**

You need a **termination rule**. Infinite calls without a stop never return a primitive. Typical stops: empty call `()`, non-number argument, or `valueOf` / `toString` for implicit conversion.

---

## 14. Memoization in JavaScript

**Memoization** caches a function’s result for the same arguments so you skip repeated expensive work.

```javascript
function memoize(fn) {
  const cache = new Map();

  return function (...args) {
    const key = JSON.stringify(args);
    if (cache.has(key)) {
      return cache.get(key);
    }
    const result = fn.apply(this, args);
    cache.set(key, result);
    return result;
  };
}

function slowFib(n) {
  if (n < 2) return n;
  return slowFib(n - 1) + slowFib(n - 2);
}

const fib = memoize(function (n) {
  if (n < 2) return n;
  return fib(n - 1) + fib(n - 2);
});

console.log(fib(40)); // fast after cache fills
```

Limits of `JSON.stringify` as a key: object key order, `undefined`, functions, circular refs. For one numeric argument, use the number itself as the Map key.

**Interview takeaway**

Memoization only helps **pure** functions (same input → same output, no side effects). It trades memory for time. See Polyfill 12 for a more complete version.

---

## 15. Rest parameter

`...` **before the last parameter** gathers leftover arguments into a real array.

```javascript
function logAll(first, ...rest) {
  console.log(first); // first argument
  console.log(rest);  // array of the rest
}

logAll("a", "b", "c");
// "a"
// ["b", "c"]
```

Rules:

- Only one rest parameter, and it must be **last**.
- It is a real `Array` (unlike the old `arguments` object).

```javascript
function sum(...nums) {
  return nums.reduce((total, n) => total + n, 0);
}

console.log(sum(1, 2, 3, 4)); // 10
```

Destructuring also uses rest:

```javascript
const [head, ...tail] = [10, 20, 30];
const { id, ...profile } = { id: 1, name: "Asha", city: "Pune" };
```

**Interview takeaway**

Rest **collects**. Spread (next topic) **expands**. Same `...` token, opposite jobs.

---

## 16. Spread operators

Spread expands an iterable (or object’s own enumerable keys) **in place**.

```javascript
const nums = [1, 2, 3];
console.log(Math.max(...nums)); // 3

const more = [0, ...nums, 4];   // [0, 1, 2, 3, 4]

const user = { name: "Asha", city: "Pune" };
const copy = { ...user, city: "Mumbai" };

function add(a, b, c) {
  return a + b + c;
}
console.log(add(...nums));
```

Shallow copy only:

```javascript
const original = { nested: { x: 1 } };
const clone = { ...original };
clone.nested.x = 99;
console.log(original.nested.x); // 99
```

Spread vs rest:

```javascript
function demo(...rest) {   // rest: gather into array
  return [...rest, 9];     // spread: expand array
}
```

**Interview takeaway**

- Arrays / strings / other iterables: `...` walks the iterator.
- Objects: copies enumerable **own** properties (ES2018).
- Spread is a shallow copy, not a deep clone.

---

## 17. How many ways can you create an object in JavaScript?

Common interview list (know at least these):

```javascript
// 1. Object literal
const a = { name: "Asha" };

// 2. new Object()
const b = new Object();
b.name = "Asha";

// 3. Constructor function
function User(name) {
  this.name = name;
}
const c = new User("Asha");

// 4. ES6 class
class Person {
  constructor(name) {
    this.name = name;
  }
}
const d = new Person("Asha");

// 5. Object.create(proto)
const e = Object.create(a);
e.age = 20;

// 6. Factory function
function createUser(name) {
  return { name, greet() { return this.name; } };
}
const f = createUser("Asha");

// 7. Object.assign
const g = Object.assign({}, { name: "Asha" });

// 8. Spread
const h = { ...a };

// 9. Array / built-in constructors (still objects)
const i = new Array(1, 2);
const j = new Date();
const k = new Map();

// 10. Object.fromEntries
const l = Object.fromEntries([
  ["name", "Asha"],
  ["city", "Pune"],
]);
```

Less common but valid: `new Function()`, JSON.parse, `structuredClone` of an existing object.

**Interview takeaway**

Literal + `new` + `Object.create` + `class` covers 90% of interviews. Mention factory and `Object.assign` / spread if they ask “how many ways.”

---

## 18. Generator functions

A **generator** is a function that can pause and resume. Calling it does **not** run the body. It returns an **iterator**.

```javascript
function* countTo(n) {
  for (let i = 1; i <= n; i++) {
    yield i;
  }
  return "done";
}

const it = countTo(3);
console.log(it.next()); // { value: 1, done: false }
console.log(it.next()); // { value: 2, done: false }
console.log(it.next()); // { value: 3, done: false }
console.log(it.next()); // { value: "done", done: true }
```

`yield` sends a value **out**. `next(x)` sends `x` **in** as the result of the previous `yield`.

```javascript
function* talk() {
  const name = yield "What is your name?";
  yield `Hello, ${name}`;
}

const g = talk();
console.log(g.next());        // question
console.log(g.next("Asha"));  // "Hello, Asha"
```

`for…of` consumes `yield` values (not the `return` value).

```javascript
for (const n of countTo(3)) {
  console.log(n); // 1, 2, 3
}
```

`yield*` delegates to another iterable/generator.

Use cases: lazy sequences, custom iterables, older async flow (`redux-saga`), infinite lists you pull from.

```javascript
function* infiniteIds() {
  let id = 1;
  while (true) {
    yield id++;
  }
}
```

**Interview takeaway**

`function*` + `yield` → iterator. Each `next()` runs until the next `yield`. Generators are **not** the same as `async` functions, though `async function*` exists for async iterators.

---

## 19. JavaScript is a single-threaded language

**Single-threaded** means one **call stack**: only one piece of your JS runs at a time.

That does **not** mean the browser or Node does only one thing. The **runtime** has:

- JS engine (stack + heap)
- Web APIs / Node APIs (timers, network, DOM, file I/O) — often in other threads
- Event loop, callback queue, microtask queue

```javascript
console.log("A");

setTimeout(() => console.log("B"), 0);

Promise.resolve().then(() => console.log("C"));

console.log("D");

// A, D, C, B
```

`setTimeout(fn, 0)` does not run `fn` immediately. It schedules work on the **macrotask** queue. The current stack must empty first.

A long synchronous loop **blocks** rendering and other JS:

```javascript
const start = Date.now();
while (Date.now() - start < 3000) {
  // page frozen for ~3 seconds
}
```

**Interview takeaway**

“JS is single-threaded” = one call stack. Concurrency comes from the event loop and async APIs, not from running two JS functions at the same instant (Workers are a separate JS world).

---

## 20. Callbacks — why we need them

A **callback** is a function you pass to another function to be called later.

We need them because JS cannot “wait” on the stack without blocking. Slow work (network, timer, disk) is started, and a callback runs when the result is ready.

```javascript
function fetchUser(id, callback) {
  setTimeout(() => {
    callback(null, { id, name: "Asha" });
  }, 200);
}

fetchUser(1, function (err, user) {
  if (err) {
    console.error(err);
    return;
  }
  console.log(user.name);
});
```

Array methods, event listeners, and timers are all callback-based:

```javascript
[1, 2, 3].map((n) => n * 2);
button.addEventListener("click", () => console.log("clicked"));
```

Problems: inversion of control (you trust the host to call you once), nesting (callback hell — Q45), error handling is easy to forget.

**Interview takeaway**

Callbacks enable non-blocking async. Promises and `async`/`await` are better APIs **on top of** the same idea.

---

## 21. Event loops

The **event loop** is the coordinator that asks: “Is the call stack empty? If yes, what should run next?”

Simplified cycle:

1. Run the current **script** (a macrotask) until the stack is empty.
2. Drain the **microtask** queue completely (Promises, `queueMicrotask`, MutationObserver).
3. Render (browsers may paint here).
4. Take the next **macrotask** (timer, I/O, UI event, `setImmediate` in Node).
5. Repeat from step 2.

```javascript
console.log("script start");

setTimeout(() => console.log("timeout"), 0);

Promise.resolve()
  .then(() => console.log("promise 1"))
  .then(() => console.log("promise 2"));

console.log("script end");

// script start
// script end
// promise 1
// promise 2
// timeout
```

**Interview takeaway**

Event loop ≠ a second thread. It is a loop that moves work from queues onto the single stack. Microtasks have higher priority than macrotasks.

---

## 22. Callback queue (macrotask / task queue)

The **callback queue** (also called **macrotask queue** or **task queue**) holds tasks such as:

- `setTimeout` / `setInterval`
- `setImmediate` (Node)
- I/O callbacks
- UI events (`click`, `keydown`)
- `requestAnimationFrame` is related but on its own rendering path in browsers

```javascript
setTimeout(() => console.log("macrotask 1"), 0);
setTimeout(() => console.log("macrotask 2"), 0);

console.log("sync");

// sync
// macrotask 1
// macrotask 2
```

Between two macrotasks, **all** microtasks run.

```javascript
setTimeout(() => {
  console.log("timeout");
  Promise.resolve().then(() => console.log("micro after timeout"));
}, 0);

Promise.resolve().then(() => console.log("micro first"));

// micro first
// timeout
// micro after timeout
```

**Interview takeaway**

“Callback queue” in older talks usually means the macrotask queue. Be precise: timers go to macrotasks; Promise `.then` goes to microtasks.

---

## 23. Microtask queue

**Microtasks** run immediately after the current call stack clears, **before** the next macrotask or paint.

Sources:

- `promise.then` / `catch` / `finally`
- `queueMicrotask(fn)`
- `MutationObserver`
- `process.nextTick` in Node (actually runs **before** the Promise microtask queue)

```javascript
queueMicrotask(() => console.log("microtask"));
Promise.resolve().then(() => console.log("promise then"));
setTimeout(() => console.log("timeout"), 0);
console.log("sync");

// sync
// microtask
// promise then
// timeout
```

A microtask can enqueue more microtasks. The loop **does not move on** until the microtask queue is empty (see Q34).

**Interview takeaway**

Microtasks starve macrotasks and rendering if they never stop. Use them for “run right after this, but after the stack,” not for heavy or unbounded work.

---

## 24. Promises

A **Promise** is an object for a future value. It is in one of three states:

- `pending`
- `fulfilled` (resolved)
- `rejected`

It **settles** once, then never changes.

```javascript
const p = new Promise((resolve, reject) => {
  const ok = true;
  if (ok) {
    resolve({ id: 1 });
  } else {
    reject(new Error("failed"));
  }
});

p.then((data) => console.log(data))
  .catch((err) => console.error(err))
  .finally(() => console.log("settled"));
```

Creating an already-settled promise:

```javascript
Promise.resolve(10);
Promise.reject(new Error("no"));
```

Chaining: each `.then` returns a **new** promise. Returned values become the next fulfillment. Thrown errors / rejected promises jump to the next `.catch`.

```javascript
Promise.resolve(2)
  .then((n) => n * 2)
  .then((n) => {
    throw new Error("boom");
  })
  .then((n) => console.log("skipped", n))
  .catch((err) => {
    console.log(err.message);
    return 0;
  })
  .then((n) => console.log("recovered", n));
```

`then` callbacks are microtasks — even on an already-resolved promise.

**Interview takeaway**

Promises fix callback hell’s pyramid and give a standard error channel. They do not make JS multi-threaded. See Q39–40 and the Promise polyfills.

---

## 25. Event propagation

When an event happens on a DOM node, it does not stay on that node. It **propagates** through the tree in three phases:

1. **Capturing** (trickle down): `window` → `document` → … → parent of target
2. **Target** phase: the node that was actually hit
3. **Bubbling** (bubble up): target → … → `document` → `window`

```
        window
           ↓ capture
        document
           ↓
         html
           ↓
         body
           ↓
        parent
           ↓
        button   ← target
           ↑ bubble
        parent
           ↑
          …
```

```html
<div id="parent">
  <button id="child">Click</button>
</div>
```

```javascript
parent.addEventListener("click", () => console.log("parent"), false);
child.addEventListener("click", () => console.log("child"), false);
// click button → "child" then "parent" (bubble, default)
```

The third argument `true` listens in the **capture** phase.

**Interview takeaway**

Propagation = capture + target + bubble. Most code uses bubble (default `false`).

---

## 26. Event bubbling

**Bubbling** is the upward phase: the event starts at the target and moves to ancestors.

```javascript
document.getElementById("list").addEventListener("click", (e) => {
  console.log("list got", e.target.tagName);
});
```

Clicks on a `<li>` inside `#list` still fire the listener on `#list`. That is bubbling.

Almost all events bubble (`click`, `input`, `keydown`). Some do not (`focus`, `blur`, `load`, `scroll` in many browsers). `focusin` / `focusout` bubble; `focus` / `blur` do not.

**Interview takeaway**

Bubbling is why a parent can hear a child’s click. It is the foundation of event delegation (Q29).

---

## 27. Event capturing

**Capturing** is the downward phase. Listeners registered with `capture: true` run **on the way down**, before target and bubble listeners.

```javascript
const parent = document.getElementById("parent");
const child = document.getElementById("child");

parent.addEventListener("click", () => console.log("parent capture"), true);
parent.addEventListener("click", () => console.log("parent bubble"), false);
child.addEventListener("click", () => console.log("child"), false);

// Click child:
// parent capture
// child
// parent bubble
```

Same node, capture vs bubble: capture listeners on that node run before bubble listeners.

Use capture when you must intercept **before** the child (analytics, stop a child from seeing the event — together with `stopPropagation`).

**Interview takeaway**

Default is bubble. Pass `true` or `{ capture: true }` for capture. Order: all captures (outer → inner), then target, then bubbles (inner → outer).

---

## 28. Event `stopPropagation`

`event.stopPropagation()` stops the event from moving to the **next** node in the remaining phases.

```javascript
child.addEventListener("click", (e) => {
  e.stopPropagation();
  console.log("child");
});

parent.addEventListener("click", () => console.log("parent"));
// Click child → only "child". Parent never hears it.
```

It does **not** stop other listeners on the **same** node.

```javascript
child.addEventListener("click", (e) => {
  e.stopPropagation();
  console.log("first");
});
child.addEventListener("click", () => console.log("second"));
// still prints first and second
```

`event.stopImmediatePropagation()` also blocks remaining listeners on the same node.

`event.preventDefault()` is different: it cancels the **browser’s default action** (form submit, link navigation). It does not stop bubbling.

**Interview takeaway**

- `stopPropagation` → don’t tell ancestors (or further capture targets).
- `stopImmediatePropagation` → also don’t tell siblings on this node.
- `preventDefault` → don’t do the native action.

---

## 29. Event delegation

**Delegation** means you attach **one** listener on a parent and use bubbling to handle many children — including children added later.

```javascript
document.getElementById("todo-list").addEventListener("click", (e) => {
  const button = e.target.closest("[data-id]");
  if (!button || !e.currentTarget.contains(button)) return;

  console.log("delete", button.dataset.id);
});
```

```html
<ul id="todo-list">
  <li><button data-id="1">Delete</button></li>
  <li><button data-id="2">Delete</button></li>
</ul>
```

Why it is better:

- Fewer listeners → less memory.
- New list items work automatically.
- Central place for the rule.

`e.target` is the deepest node clicked (maybe a `<span>` inside the button). `e.currentTarget` is the node with the listener (`#todo-list`). `closest` walks up to the real control.

**Interview takeaway**

Delegation = parent listener + bubbling + `target` / `closest`. Do not attach a click handler to every row in a 10,000-row table.

---

## 30. Type coercion vs conversion

**Conversion** (explicit) is when **you** change a type on purpose.

```javascript
Number("42");      // 42
String(42);        // "42"
Boolean(1);        // true
parseInt("42px", 10);
!!"hi";            // true
+ "7";             // 7 — short, but still a conversion you wrote
```

**Coercion** (implicit) is when the **engine** changes a type to make an operation work.

```javascript
"5" + 1      // "51"  string wins for +
"5" - 1      // 4     - forces numbers
1 == "1"     // true
if ("") {}   // "" is falsy
true + true  // 2
[] + []      // ""
[] + {}      // "[object Object]"
{} + []      // 0 in some contexts (statement vs expression)
```

ToPrimitive / valueOf / toString participate in coercion.

Falsy values: `false`, `0`, `-0`, `0n`, `""`, `null`, `undefined`, `NaN`.

**Interview takeaway**

Conversion = you asked. Coercion = JS asked. Prefer explicit conversion in code; explain implicit rules in interviews (`==`, `+`, `if (value)`).

---

## 31. Throttle

**Throttle** guarantees a function runs **at most once** in every `wait` milliseconds while events keep firing.

Typical use: scroll, resize, mousemove — you want a steady stream of updates, not one update after the user stops.

```javascript
function throttle(fn, wait) {
  let last = 0;
  let timer = null;

  return function (...args) {
    const now = Date.now();
    const remaining = wait - (now - last);
    const context = this;

    if (remaining <= 0) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      last = now;
      fn.apply(context, args);
    } else if (!timer) {
      timer = setTimeout(() => {
        last = Date.now();
        timer = null;
        fn.apply(context, args);
      }, remaining);
    }
  };
}

window.addEventListener(
  "scroll",
  throttle(() => console.log(window.scrollY), 200)
);
```

**Interview takeaway**

Throttle = “regular heartbeat.” Debounce = “wait until quiet.” See Polyfill 6 for a full version.

---

## 32. Debounce

**Debounce** waits until the function has **not** been called for `wait` ms, then runs it once.

Typical use: search box, window resize end, button double-click guard.

```javascript
function debounce(fn, wait) {
  let timer = null;

  return function (...args) {
    const context = this;
    clearTimeout(timer);
    timer = setTimeout(() => {
      fn.apply(context, args);
    }, wait);
  };
}

const search = debounce((q) => {
  console.log("API call for", q);
}, 300);

search("a");
search("ap");
search("app");
// only "app" fires after 300ms of silence
```

Leading debounce (run immediately, then ignore until quiet) is a common follow-up — see Polyfill 5.

**Interview takeaway**

If the interviewer says “don’t hit the API on every keystroke,” they want debounce. If they say “update the position while dragging, but not 100 times per second,” they want throttle.

---

## 33. How JavaScript parses and compiles your code (step by step)

Modern engines (V8: Chrome / Node) are **JIT** compilers. They do not only “interpret line by line.”

### Step 1 — Load the source

The file or `<script>` text arrives as a string (UTF-16 / UTF-8 decoded).

### Step 2 — Lexing (tokenization)

The scanner splits source into tokens: `function`, `ident`, `(`, `{`, `let`, `=`, `10`, …

Syntax errors can appear here (`let let = 1` in sloppy ways, bad characters).

### Step 3 — Parsing → AST

Tokens become an **Abstract Syntax Tree**. The parser also builds **scopes** (which names exist, `var` vs `let`, inner functions).

A parse error (`}` missing) throws before any of your code runs.

### Step 4 — Early errors and bytecode

The engine walks the AST and emits **bytecode** (V8’s interpreter is **Ignition**).

This is also when:

- function declarations are recorded
- `var` slots are created
- TDZ flags for `let` / `const` exist

### Step 5 — Execution (interpreter)

Bytecode runs. Hidden classes / shapes of objects are observed. Types of locals are profiled (“`n` is always a number”).

### Step 6 — Optimizing compiler (JIT)

Hot functions are sent to an optimizing compiler (**TurboFan** in V8). It emits fast machine code assuming the profiled types stay true.

### Step 7 — Deoptimization

If an assumption breaks (`n` suddenly becomes a string), the engine **deopts** back to bytecode / a safer version. That is why “polymorphic” hot code can get slower.

### Step 8 — Garbage collection (ongoing)

Unused objects are collected on the heap while this happens (Q51).

```
Source
  → tokens
  → AST + scopes
  → bytecode (Ignition)
  → run + profile
  → optimized machine code (TurboFan)
  → maybe deopt
```

**Interview takeaway**

JS is compiled **and** interpreted. First run is usually bytecode. Hot paths get machine code. `eval` and `with` hurt optimization because they make scopes unpredictable.

---

## 34. Infinite microtasks — how do you handle them?

If a microtask always enqueues another microtask, the engine **never** reaches the next macrotask or paint. The page freezes. Timers never fire.

```javascript
function flood() {
  Promise.resolve().then(flood);
}
flood();
// setTimeout callbacks will not run
```

### How to handle it

**1. Do not schedule unbounded microtasks.**  
Fix the algorithm. A recursive `then` that always continues is a bug.

**2. Yield to the macrotask queue.**  
After N items, continue via `setTimeout`, `setImmediate` (Node), or `MessageChannel` / `scheduler.yield()` so the event loop can paint and run timers.

```javascript
async function processAll(items) {
  for (let i = 0; i < items.length; i++) {
    doWork(items[i]);

    if (i % 100 === 0) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  }
}
```

**3. Use a generation / token to stop.**

```javascript
let generation = 0;

function startWork() {
  const myGen = ++generation;

  function step() {
    if (myGen !== generation) return; // cancelled
    // …do a slice…
    Promise.resolve().then(step); // still dangerous if step never stops
  }

  step();
}
```

Prefer yielding with a macrotask if the loop is long.

**4. Hard stop in Node.**  
`process.nextTick` flooding is even worse (it runs before Promises). Use `setImmediate` to break the cycle.

**Interview takeaway**

The spec says: drain microtasks until empty. There is no built-in “fairness.” You handle it by **yielding** (`setTimeout(0)`, `scheduler.yield`) or **not enqueueing** the next microtask.

---

## 35. First-class functions

Functions are **first-class** when they are values: you can store them, pass them, and return them.

```javascript
const greet = function (name) {
  return `Hi ${name}`;
};

function applyTwice(fn, value) {
  return fn(fn(value));
}

function makeMultiplier(n) {
  return function (x) {
    return x * n;
  };
}

const double = makeMultiplier(2);
console.log(applyTwice(double, 3)); // 12

const ops = {
  add: (a, b) => a + b,
};

setTimeout(() => console.log(greet("Asha")), 0);
```

This is what enables callbacks, HOFs, closures, currying, and promises.

**Interview takeaway**

First-class = functions are data. JavaScript has this. Java (historically) did not treat methods this way; that contrast sometimes appears in interviews.

---

## 36. Immediately Invoked Function Expressions (IIFE)

An **IIFE** is a function that runs as soon as it is defined.

```javascript
(function () {
  var privateCounter = 0;
  console.log("runs immediately");
})();

(() => {
  console.log("arrow IIFE");
})();

const result = (function (a, b) {
  return a + b;
})(2, 3);
```

Why people used them (especially before modules / `let`):

- Create a **private scope** so `var` does not leak to global.
- Snapshot a value in a loop (`(function (i) { … })(i)`).
- Run setup code once.

The wrapping `()` is required so `function` is parsed as an **expression**, not a declaration.

```javascript
(function named() {
  // name is local to the IIFE
})();
```

**Interview takeaway**

IIFE = define + call in one expression. Still useful for isolation; ES modules and block-scoped `let` / `const` replaced most old use cases.

---

## 37. `call`, `apply`, and `bind` — why we use them

They all set `this` for a function. They differ in **when** and **how arguments** are passed.

| | Sets `this` | Arguments | Runs |
|---|---|---|---|
| `call` | Yes | Comma list | Immediately |
| `apply` | Yes | Array | Immediately |
| `bind` | Yes | Partial args | Returns a new function |

```javascript
function introduce(city, country) {
  return `${this.name} from ${city}, ${country}`;
}

const user = { name: "Asha" };

console.log(introduce.call(user, "Pune", "India"));
console.log(introduce.apply(user, ["Pune", "India"]));

const bound = introduce.bind(user, "Pune");
console.log(bound("India"));
```

Why we use them:

1. **Borrow a method** — `Array.prototype.slice.call(arguments)` (older code).
2. **Fix `this`** when passing a method as a callback (`obj.method` loses `this`).
3. **Partial application** with `bind`.
4. **Constructors** — `Parent.call(this, args)` in inheritance.

```javascript
const button = {
  label: "Save",
  click() {
    console.log(this.label);
  },
};

// Lost this:
setTimeout(button.click, 0);           // undefined (or window)
setTimeout(button.click.bind(button), 0); // "Save"
```

`apply` is rarely needed now because of spread: `fn.call(this, ...arr)`.

**Interview takeaway**

`bind` does not call. `call` / `apply` do. `bind` is sticky: later `call` on the bound function cannot change `this` (bound `this` is permanent).

---

## 38. MapLimit

**mapLimit** (from the `async` library idea) maps over a list with an **async** iterator, but only `limit` operations run at the same time.

You need it when you have 1,000 URLs and must not open 1,000 connections.

```javascript
function mapLimit(items, limit, iteratee) {
  return new Promise((resolve, reject) => {
    if (!items.length) {
      resolve([]);
      return;
    }

    const results = new Array(items.length);
    let nextIndex = 0;
    let inFlight = 0;
    let finished = 0;
    let failed = false;

    function launch() {
      while (inFlight < limit && nextIndex < items.length && !failed) {
        const current = nextIndex;
        nextIndex += 1;
        inFlight += 1;

        Promise.resolve()
          .then(() => iteratee(items[current], current))
          .then((value) => {
            results[current] = value;
            inFlight -= 1;
            finished += 1;
            if (finished === items.length) {
              resolve(results);
            } else {
              launch();
            }
          })
          .catch((err) => {
            failed = true;
            reject(err);
          });
      }
    }

    launch();
  });
}

async function fakeFetch(url) {
  await new Promise((r) => setTimeout(r, 100));
  return `body:${url}`;
}

mapLimit(["/a", "/b", "/c", "/d"], 2, fakeFetch).then(console.log);
```

**Interview takeaway**

This is a concurrency pool: keep `limit` promises in flight, start the next when one finishes, preserve result order. Full polyfill is in Part B.

---

## 39. `async` and `await`

`async` / `await` is syntax over Promises. An `async` function always returns a Promise.

```javascript
async function loadUser(id) {
  const res = await fetch(`/api/users/${id}`);
  if (!res.ok) {
    throw new Error("HTTP " + res.status);
  }
  const data = await res.json();
  return data;
}

loadUser(1)
  .then((user) => console.log(user))
  .catch((err) => console.error(err));
```

`await` pauses **that function** (not the whole thread). The engine returns to the event loop. When the promise settles, the rest of the function is queued as a microtask.

Errors: a rejected `await` throws. Use `try/catch`.

```javascript
async function safe() {
  try {
    await Promise.reject(new Error("nope"));
  } catch (err) {
    console.log(err.message);
  }
}
```

Do not `await` in a loop when work can run in parallel:

```javascript
// slow — sequential
for (const url of urls) {
  await fetch(url);
}

// fast — parallel
await Promise.all(urls.map((url) => fetch(url)));
```

**Interview takeaway**

`async`/`await` is still Promises + microtasks. It is not a new thread. `await` on a non-promise wraps the value with `Promise.resolve`.

---

## 40. `then` and `catch`

`.then(onFulfilled, onRejected)` and `.catch(onRejected)` attach handlers that run as **microtasks**.

```javascript
Promise.resolve(1)
  .then((n) => n + 1)
  .then((n) => {
    console.log(n); // 2
    return n;
  });

Promise.reject(new Error("fail"))
  .catch((err) => {
    console.log(err.message);
    return "ok";
  })
  .then((msg) => console.log(msg)); // "ok" — catch recovered
```

Rules worth memorizing:

1. `.then` with one argument ignores rejections (they skip to the next `catch`).
2. `.catch(fn)` is `then(undefined, fn)`.
3. If `onFulfilled` throws, the next promise rejects.
4. If `onRejected` / `catch` returns a value, the chain is **fulfilled** again.
5. Always return or throw inside `then` — forgetting `return` passes `undefined`.
6. A rejected promise with no `catch` becomes an unhandled rejection.

```javascript
Promise.resolve()
  .then(() => {
    throw new Error("x");
  })
  .then(() => console.log("skip"))
  .catch(() => console.log("handled"));
```

Two-argument `then` vs `catch` after: if you handle the error in the second argument, a later `catch` may not see it.

**Interview takeaway**

`then` transforms fulfillment. `catch` transforms rejection. Both return a new promise. That is why chaining works.

---

## 41. Variable shadowing

**Shadowing** is when an inner binding uses the **same name** as an outer one. The inner name hides the outer name in that scope.

```javascript
let name = "outer";

function demo() {
  let name = "inner";
  console.log(name); // "inner"
}

demo();
console.log(name); // "outer"
```

Illegal / surprising cases:

```javascript
let x = 1;
{
  // console.log(x); // TDZ if the next line exists
  let x = 2;        // shadows outer x
}

var y = 1;
function wrap() {
  var y = 2; // shadows
  console.log(y);
}

// Illegal shadowing with let after var in the *same* function scope:
function bad() {
  var a = 1;
  // let a = 2; // SyntaxError in this scope
}
```

`const` / `let` in a block can shadow a function-level `var`.

**Interview takeaway**

Shadowing is scope chaining with a name collision. The inner binding wins. It does not overwrite the outer variable.

---

## 42. What does `static` mean in a JavaScript class?

`static` members belong to the **class (constructor function)**, not to instances.

```javascript
class User {
  constructor(name) {
    this.name = name;
  }

  greet() {
    return this.name;
  }

  static species = "human";

  static fromJSON(json) {
    const data = JSON.parse(json);
    return new User(data.name);
  }
}

const u = new User("Asha");
console.log(u.greet());          // instance method
console.log(User.species);       // "human"
console.log(User.fromJSON('{"name":"Dev"}').name);
console.log(u.species);          // undefined
```

Static methods are inherited by **subclasses**, not by instances:

```javascript
class Admin extends User {
  static fromJSON(json) {
    const user = super.fromJSON(json);
    return new Admin(user.name);
  }
}

console.log(Admin.species); // "human"
```

Under the hood: `User.fromJSON = function…` on the constructor, not on `User.prototype`.

**Interview takeaway**

`static` = factory helpers, counters, constants on the class. Call `Class.method()`, not `instance.method()`.

---

## 43. `undefined` vs not-defined vs `null`

These are three different situations.

### `undefined`

The variable **exists**, but has no value assigned (or a function returned nothing, or a missing object property).

```javascript
let a;
console.log(a);          // undefined
console.log({}.x);       // undefined

function f() {}
console.log(f());        // undefined

typeof a;                // "undefined"
```

### Not defined (`ReferenceError`)

The name **does not exist** in the scope chain.

```javascript
console.log(missing);    // ReferenceError: missing is not defined
typeof missing;          // "undefined" — typeof is safe for missing names
```

### `null`

A value you assign to mean **“intentional empty.”** It is a primitive.

```javascript
let user = null;         // we know there is no user yet
typeof null;             // "object" (bug)
null == undefined;       // true
null === undefined;      // false
```

| | Exists as binding? | Has a value? | Typical cause |
|---|---|---|---|
| `undefined` | Yes | The value `undefined` | Uninitialized, missing prop, no `return` |
| not defined | No | — | Typo / never declared |
| `null` | Yes | The value `null` | Programmer said “empty” |

**Interview takeaway**

`undefined` is a value. “Not defined” is an error. `null` is an intentional empty object reference. Never use them interchangeably in APIs if you can help it.

---

## 44. Higher-order functions

A **higher-order function (HOF)** takes a function as an argument, returns a function, or both.

```javascript
function map(arr, fn) {
  const out = [];
  for (let i = 0; i < arr.length; i++) {
    out.push(fn(arr[i], i));
  }
  return out;
}

console.log(map([1, 2, 3], (n) => n * 2));

function withLogging(fn) {
  return function (...args) {
    console.log("args", args);
    return fn(...args);
  };
}
```

Built-in HOFs: `map`, `filter`, `reduce`, `forEach`, `then`, `setTimeout`, `addEventListener`.

**Interview takeaway**

HOF is possible because functions are first-class. Closures are how returned HOFs remember configuration.

---

## 45. Callback hell

**Callback hell** is deeply nested callbacks that become hard to read, hard to error-handle, and hard to reuse.

```javascript
getUser(1, function (err, user) {
  if (err) return show(err);
  getOrders(user.id, function (err, orders) {
    if (err) return show(err);
    getOrder(orders[0].id, function (err, order) {
      if (err) return show(err);
      getProduct(order.productId, function (err, product) {
        if (err) return show(err);
        console.log(product);
      });
    });
  });
});
```

Fixes:

1. **Named functions** instead of anonymous nests.
2. **Promises** — flat chain.
3. **`async`/`await`** — looks synchronous.

```javascript
async function load() {
  const user = await getUser(1);
  const orders = await getOrders(user.id);
  const order = await getOrder(orders[0].id);
  const product = await getProduct(order.productId);
  console.log(product);
}
```

**Interview takeaway**

The pain is nesting + duplicated error handling + inversion of control. Promises flatten the pyramid; they do not remove asynchrony.

---

## 46. `this` in JavaScript

`this` is set by **how a function is called**, not where it is written (except arrows).

| Call style | `this` (non-strict) | `this` (strict) |
|---|---|---|
| `fn()` | `window` / `global` | `undefined` |
| `obj.fn()` | `obj` | `obj` |
| `fn.call(x)` / `apply` / `bind` | `x` | `x` |
| `new Fn()` | the new instance | the new instance |
| Arrow function | lexical `this` (enclosing scope) | same |

```javascript
const user = {
  name: "Asha",
  regular() {
    return this.name;
  },
  arrow: () => this.name,
};

console.log(user.regular()); // "Asha"
console.log(user.arrow());   // undefined (or global name) — arrow took outer this

const detached = user.regular;
console.log(detached());     // undefined in strict / modules
```

Class bodies are strict. Methods lose `this` when extracted.

```javascript
class Counter {
  count = 0;
  inc() {
    this.count++;
  }
  incArrow = () => {
    this.count++;
  };
}

const c = new Counter();
const f = c.inc;
// f(); // TypeError — this is undefined
c.incArrow(); // OK — arrow closed over the instance
```

DOM handlers: `this` is the element for a regular function listener, not for an arrow (arrow uses outer `this`).

**Interview takeaway**

Four bindings: default, implicit (`obj.`), explicit (`call`/`bind`), `new`. Arrows skip all four and use lexical `this`.

---

## 47. Function declaration, expression, anonymous functions, arrow functions

```javascript
// 1. Declaration — hoisted with body
function add(a, b) {
  return a + b;
}

// 2. Expression — assigned to a variable (named or anonymous)
const sub = function sub(a, b) {
  return a - b;
};

// 3. Anonymous function expression
const mul = function (a, b) {
  return a * b;
};

// 4. Arrow function — short, lexical this, no own `arguments`, not constructable
const div = (a, b) => a / b;

const obj = {
  nums: [1],
  double() {
    return this.nums.map((n) => n * 2); // this is obj
  },
};
```

Differences interviewers ask:

| | Declaration | Expression | Arrow |
|---|---|---|---|
| Hoisted | Yes | Variable only | Variable only |
| `this` | Dynamic | Dynamic | Lexical |
| `new` | Yes | Yes | No |
| `arguments` | Yes | Yes | No (use rest) |
| Own `prototype` | Yes | Yes | No |

```javascript
const Arrow = () => {};
// new Arrow(); // TypeError
```

Anonymous functions have no `.name` (or inferred name from the variable in modern engines).

**Interview takeaway**

Use declarations for named top-level helpers. Use arrows for callbacks that should keep outer `this`. Do not use arrows as object methods if you need `this` to be the object — unless the method is defined as a class field arrow on purpose.

---

## 48. `async` vs `defer` on `<script>`

Both download the script **without blocking HTML parse** (for classic scripts). They differ in **when the file runs**.

```html
<script src="a.js"></script>          <!-- parser waits; runs immediately -->
<script src="b.js" async></script>    <!-- download parallel; runs as soon as ready -->
<script src="c.js" defer></script>    <!-- download parallel; runs after HTML is parsed -->
```

| | Parser blocked while downloading? | When it runs | Order |
|---|---|---|---|
| Normal | Yes | As soon as downloaded, parser paused | Document order |
| `async` | No | As soon as downloaded, may pause parser to run | **Arrival order** (unpredictable) |
| `defer` | No | After document is parsed, before `DOMContentLoaded` | **Document order** |

Rules:

- `defer` is for scripts that need the DOM and may depend on each other (`app.js` after `vendor.js`).
- `async` is for independent scripts (analytics). Do not assume order.
- `type="module"` scripts **defer by default**. `async` on a module means run as soon as the module graph is ready, not waiting for the document.

**Interview takeaway**

`defer` = preserve order, run after parse. `async` = run ASAP, order not guaranteed. That is the whole question.

---

## 49. Execution context

An **execution context** is the environment in which a piece of JS runs. There are:

- **Global execution context** (one per realm)
- **Function execution context** (every function call)
- **Eval** context (rare)

Each context has:

1. **Variable environment** / **lexical environment** — bindings (`var`, `let`, `const`, functions, arguments).
2. **`this` binding**.
3. **Outer** reference to the parent lexical environment.

Two phases:

**Creation**

- Create the lexical environment.
- Hoist `var` to `undefined`.
- Hoist function declarations.
- Create `let` / `const` bindings in TDZ.
- Bind `this` and `arguments`.

**Execution**

- Run the code line by line.
- Assign values, leave TDZ, call functions (push new contexts).

```javascript
var x = 10;

function outer() {
  var y = 20;
  function inner() {
    console.log(x, y);
  }
  inner();
}

outer();
```

Stack of contexts: `global` → `outer` → `inner`. When `inner` returns, it is popped.

**Interview takeaway**

Every call creates a new function execution context. That is why two calls to `makeCounter()` get two different `count` variables.

---

## 50. Call stack

The **call stack** is a LIFO stack of execution contexts.

```javascript
function one() {
  two();
}

function two() {
  three();
}

function three() {
  console.log("bottom");
}

one();
```

```
three  ← top (running)
two
one
global
```

When `three` returns, it is popped, then `two`, then `one`.

**Stack overflow** happens when recursion never returns:

```javascript
function recurse() {
  recurse();
}
// RangeError: Maximum call stack size exceeded
```

The event loop only starts a new macrotask / microtask when the stack is **empty** (or, for microtasks, when the current script/function stack has cleared).

**Interview takeaway**

Synchronous calls grow the stack. `setTimeout` and `then` do **not** grow the current stack — they schedule a later turn with a fresh stack.

---

## 51. Garbage collection

**Garbage collection (GC)** frees heap memory that your program can no longer reach.

JavaScript uses **reachability**, not “reference counting only”:

- Roots: global object, current stack variables, pending closures, DOM nodes still in the tree.
- Anything reachable from a root stays alive.
- Unreachable objects are collected.

```javascript
let user = { name: "Asha" };
user = null; // previous object may be collected
```

Closures keep variables alive:

```javascript
function hold() {
  const huge = new Array(1e6).fill("x");
  return function () {
    return huge[0]; // huge cannot be collected while this function lives
  };
}
```

**Mark-and-sweep** (main idea): mark reachable objects from roots, sweep the rest.

Engines also use **generational GC**: young objects die fast (minor GC); survivors move to an old space (major GC, rarer, more expensive).

Memory leaks in JS apps:

- Forgotten event listeners / intervals
- Closures holding large data
- Global caches that only grow
- Detached DOM nodes still referenced from JS

**Interview takeaway**

You do not free memory manually. You drop references. GC is not instant. Leaks are “still reachable when you thought they were not.”

---

## 52. Equality (`===` vs `==`)

`===` **strict equality**: same type and same value. No coercion.

```javascript
1 === 1        // true
1 === "1"      // false
true === 1     // false
null === undefined // false
NaN === NaN    // false
+0 === -0      // true
```

`==` **loose equality**: coerce, then compare.

```javascript
1 == "1"           // true
true == 1          // true
null == undefined  // true
null == 0          // false
"" == 0            // true
[] == false        // true
[] == ""           // true
```

Objects: both operators compare **references**, not deep content.

```javascript
{} == {}           // false
const a = {};
a === a            // true
```

Use `Object.is` for the remaining edge cases:

```javascript
Object.is(NaN, NaN); // true
Object.is(+0, -0);   // false
```

**Interview takeaway**

Always use `===` in application code. Know `==` tables for interviews. The only common intentional `==` is `x == null` (true for both `null` and `undefined`).

---

## 53. Strict mode

`"use strict"` opts into a safer, quieter language subset. ES modules and `class` bodies are strict by default.

```javascript
"use strict";

// x = 10;           // ReferenceError — no implicit globals
// function f(a, a) {} // SyntaxError — duplicate params
// false.x = 1;      // TypeError — cannot create props on primitives
// delete Object.prototype; // TypeError

function show() {
  console.log(this); // undefined, not window
}
show();
```

What strict mode changes (high yield for interviews):

1. Assignment to undeclared names throws.
2. `this` is `undefined` in bare calls.
3. Duplicate parameter names are illegal.
4. `with` is illegal.
5. `eval` / `arguments` behave less magically (`arguments` does not alias parameters).
6. `delete` of a non-configurable or bare identifier throws.
7. Octal literals like `0123` are illegal.
8. `implements`, `interface`, `let` as identifiers, etc., are reserved.

Enable it:

```javascript
"use strict";          // whole file / script

function onlyThis() {
  "use strict";        // one function
}
```

**Interview takeaway**

Strict mode exists to catch silent mistakes. In modern `type="module"` code you already have it.

---

## 54. Lexical environment

A **lexical environment** is the hidden object (spec-level) that holds:

- an **environment record** (the actual variable map)
- a reference to the **outer** lexical environment

This is the formal model behind scope and closures.

```javascript
const globalX = 1;

function outer() {
  const a = 2;

  function inner() {
    const b = 3;
    console.log(globalX, a, b);
  }

  return inner;
}

const fn = outer();
fn();
```

When `inner` runs, its lexical environment looks like:

```
inner env:  { b: 3 }  →  outer env: { a: 2, inner }  →  global env: { globalX, outer, fn }
```

`outer` has already returned, but its environment stays alive because `inner`’s outer pointer still points there. That retained environment **is** the closure.

Kinds of environment records you may hear:

- **Declarative** — `let`, `const`, `function`, `class`, parameters
- **Object** — global bindings that map onto the global object (`var` in scripts)
- **Function** — also has `arguments` and `this` (this-binding is on the context, but people mention them together)

**Interview takeaway**

Lexical environment = scope object + parent link. Scope chain = walking those parent links. Closure = a function keeping its lexical environment after the caller finished.

---

# Part B — Polyfills

Implement these from scratch in interviews. Each section: what the native API does, then a readable polyfill, then usage.

These are **teaching polyfills**. They omit some spec edge cases (holes in sparse arrays, species constructors, `thisArg` on every path). Mention that if the interviewer is senior.

---

## Polyfill 1 — `map`, `reduce`, `filter`, `forEach`, `find`

```javascript
Array.prototype.myMap = function (callback, thisArg) {
  if (typeof callback !== "function") {
    throw new TypeError(callback + " is not a function");
  }

  const source = Object(this);
  const length = source.length >>> 0;
  const result = new Array(length);

  for (let i = 0; i < length; i++) {
    if (i in source) {
      result[i] = callback.call(thisArg, source[i], i, source);
    }
  }
  return result;
};

Array.prototype.myReduce = function (callback, initialValue) {
  if (typeof callback !== "function") {
    throw new TypeError(callback + " is not a function");
  }

  const source = Object(this);
  const length = source.length >>> 0;
  let i = 0;
  let acc;

  if (arguments.length >= 2) {
    acc = initialValue;
  } else {
    while (i < length && !(i in source)) {
      i++;
    }
    if (i >= length) {
      throw new TypeError("Reduce of empty array with no initial value");
    }
    acc = source[i++];
  }

  for (; i < length; i++) {
    if (i in source) {
      acc = callback(acc, source[i], i, source);
    }
  }
  return acc;
};

Array.prototype.myFilter = function (callback, thisArg) {
  if (typeof callback !== "function") {
    throw new TypeError(callback + " is not a function");
  }

  const source = Object(this);
  const length = source.length >>> 0;
  const result = [];

  for (let i = 0; i < length; i++) {
    if (i in source) {
      const value = source[i];
      if (callback.call(thisArg, value, i, source)) {
        result.push(value);
      }
    }
  }
  return result;
};

Array.prototype.myForEach = function (callback, thisArg) {
  if (typeof callback !== "function") {
    throw new TypeError(callback + " is not a function");
  }

  const source = Object(this);
  const length = source.length >>> 0;

  for (let i = 0; i < length; i++) {
    if (i in source) {
      callback.call(thisArg, source[i], i, source);
    }
  }
};

Array.prototype.myFind = function (callback, thisArg) {
  if (typeof callback !== "function") {
    throw new TypeError(callback + " is not a function");
  }

  const source = Object(this);
  const length = source.length >>> 0;

  for (let i = 0; i < length; i++) {
    if (i in source) {
      const value = source[i];
      if (callback.call(thisArg, value, i, source)) {
        return value;
      }
    }
  }
  return undefined;
};

const nums = [1, 2, 3, 4];
console.log(nums.myMap((n) => n * 2));           // [2, 4, 6, 8]
console.log(nums.myFilter((n) => n % 2 === 0));  // [2, 4]
console.log(nums.myReduce((a, n) => a + n, 0));  // 10
nums.myForEach((n) => console.log(n));
console.log(nums.myFind((n) => n > 2));          // 3
```

Why `i in source`: sparse arrays (`[1, , 3]`) should skip holes, like the real methods.

`length >>> 0` converts length to a valid uint32, matching the spec style.

---

## Polyfill 2 — `call`, `apply`, `bind`

Idea for `call` / `apply`: temporarily attach the function to the given `this` value and invoke it.

```javascript
Function.prototype.myCall = function (thisArg, ...args) {
  if (typeof this !== "function") {
    throw new TypeError("myCall must be called on a function");
  }

  const context =
    thisArg === null || thisArg === undefined
      ? globalThis
      : Object(thisArg);

  const key = Symbol("fn");
  context[key] = this;
  const result = context[key](...args);
  delete context[key];
  return result;
};

Function.prototype.myApply = function (thisArg, args) {
  const list = args == null ? [] : Array.from(args);
  return this.myCall(thisArg, ...list);
};

Function.prototype.myBind = function (thisArg, ...boundArgs) {
  if (typeof this !== "function") {
    throw new TypeError("myBind must be called on a function");
  }

  const original = this;

  function bound(...args) {
    const isConstruct = this instanceof bound;
    return original.apply(isConstruct ? this : thisArg, boundArgs.concat(args));
  }

  if (original.prototype) {
    bound.prototype = Object.create(original.prototype);
  }

  return bound;
};

function greet(city) {
  return `${this.name} ${city}`;
}

console.log(greet.myCall({ name: "Asha" }, "Pune"));
console.log(greet.myApply({ name: "Asha" }, ["Pune"]));

const g = greet.myBind({ name: "Asha" }, "Pune");
console.log(g());

function Person(name) {
  this.name = name;
}
const BoundPerson = Person.myBind({});
const p = new BoundPerson("Dev");
console.log(p.name); // "Dev" — new ignores bound this
```

`bind` + `new`: if the bound function is used as a constructor, `this` should be the new instance, not the bound object. That is why we check `this instanceof bound`.

---

## Polyfill 3 — `Promise`, `all`, `any`, `allSettled`, `race`

A minimal Promise that is enough for interviews: states, `then` chaining, microtask scheduling.

```javascript
const PENDING = "pending";
const FULFILLED = "fulfilled";
const REJECTED = "rejected";

function MyPromise(executor) {
  this.state = PENDING;
  this.value = undefined;
  this.reason = undefined;
  this.onFulfilled = [];
  this.onRejected = [];

  const resolve = (value) => {
    if (this.state !== PENDING) return;

    if (value instanceof MyPromise) {
      value.then(resolve, reject);
      return;
    }

    this.state = FULFILLED;
    this.value = value;
    queueMicrotask(() => {
      this.onFulfilled.forEach((fn) => fn());
    });
  };

  const reject = (reason) => {
    if (this.state !== PENDING) return;
    this.state = REJECTED;
    this.reason = reason;
    queueMicrotask(() => {
      this.onRejected.forEach((fn) => fn());
    });
  };

  try {
    executor(resolve, reject);
  } catch (err) {
    reject(err);
  }
}

MyPromise.prototype.then = function (onFulfilled, onRejected) {
  onFulfilled = typeof onFulfilled === "function" ? onFulfilled : (v) => v;
  onRejected =
    typeof onRejected === "function"
      ? onRejected
      : (e) => {
          throw e;
        };

  const run = (cb, value, resolve, reject) => {
    queueMicrotask(() => {
      try {
        const result = cb(value);
        resolve(result);
      } catch (err) {
        reject(err);
      }
    });
  };

  return new MyPromise((resolve, reject) => {
    const handleFulfill = () => run(onFulfilled, this.value, resolve, reject);
    const handleReject = () => run(onRejected, this.reason, resolve, reject);

    if (this.state === FULFILLED) {
      handleFulfill();
    } else if (this.state === REJECTED) {
      handleReject();
    } else {
      this.onFulfilled.push(handleFulfill);
      this.onRejected.push(handleReject);
    }
  });
};

MyPromise.prototype.catch = function (onRejected) {
  return this.then(null, onRejected);
};

MyPromise.prototype.finally = function (onFinally) {
  return this.then(
    (value) => MyPromise.resolve(onFinally()).then(() => value),
    (reason) =>
      MyPromise.resolve(onFinally()).then(() => {
        throw reason;
      })
  );
};

MyPromise.resolve = function (value) {
  if (value instanceof MyPromise) return value;
  return new MyPromise((resolve) => resolve(value));
};

MyPromise.reject = function (reason) {
  return new MyPromise((_, reject) => reject(reason));
};

MyPromise.all = function (promises) {
  return new MyPromise((resolve, reject) => {
    const list = Array.from(promises);
    const result = [];
    let remaining = list.length;

    if (remaining === 0) {
      resolve([]);
      return;
    }

    list.forEach((p, i) => {
      MyPromise.resolve(p).then((value) => {
        result[i] = value;
        remaining -= 1;
        if (remaining === 0) resolve(result);
      }, reject);
    });
  });
};

MyPromise.race = function (promises) {
  return new MyPromise((resolve, reject) => {
    for (const p of promises) {
      MyPromise.resolve(p).then(resolve, reject);
    }
  });
};

MyPromise.any = function (promises) {
  return new MyPromise((resolve, reject) => {
    const list = Array.from(promises);
    if (list.length === 0) {
      reject(new AggregateError([], "All promises were rejected"));
      return;
    }

    const errors = [];
    let rejected = 0;

    list.forEach((p, i) => {
      MyPromise.resolve(p).then(resolve, (err) => {
        errors[i] = err;
        rejected += 1;
        if (rejected === list.length) {
          reject(new AggregateError(errors, "All promises were rejected"));
        }
      });
    });
  });
};

MyPromise.allSettled = function (promises) {
  return new MyPromise((resolve) => {
    const list = Array.from(promises);
    const result = [];
    let remaining = list.length;

    if (remaining === 0) {
      resolve([]);
      return;
    }

    list.forEach((p, i) => {
      MyPromise.resolve(p).then(
        (value) => {
          result[i] = { status: "fulfilled", value };
          remaining -= 1;
          if (remaining === 0) resolve(result);
        },
        (reason) => {
          result[i] = { status: "rejected", reason };
          remaining -= 1;
          if (remaining === 0) resolve(result);
        }
      );
    });
  });
};
```

Quick memory table:

| Helper | Succeeds when | Fails when |
|---|---|---|
| `all` | every promise fulfills | first rejection |
| `race` | first to settle | first to settle (reject or fulfill) |
| `any` | first fulfillment | **all** reject (`AggregateError`) |
| `allSettled` | all settled | never rejects for child failures |

---

## Polyfill 4 — `mapLimit`

Same idea as Q38, written as a reusable helper. Preserves order. Stops on first error.

```javascript
function mapLimit(iterable, limit, iteratee) {
  const items = Array.from(iterable);

  if (limit < 1) {
    return Promise.reject(new RangeError("limit must be >= 1"));
  }

  return new Promise((resolve, reject) => {
    const results = new Array(items.length);
    let next = 0;
    let running = 0;
    let done = 0;
    let stopped = false;

    if (items.length === 0) {
      resolve([]);
      return;
    }

    const kick = () => {
      while (running < limit && next < items.length && !stopped) {
        const index = next;
        next += 1;
        running += 1;

        Promise.resolve()
          .then(() => iteratee(items[index], index, items))
          .then((value) => {
            results[index] = value;
            running -= 1;
            done += 1;
            if (done === items.length) {
              resolve(results);
            } else {
              kick();
            }
          })
          .catch((err) => {
            stopped = true;
            reject(err);
          });
      }
    };

    kick();
  });
}

// Example: at most 2 tasks at a time
function delay(ms, value) {
  return new Promise((r) => setTimeout(() => r(value), ms));
}

mapLimit([1, 2, 3, 4], 2, (n) => delay(80, n * 10)).then(console.log);
// [10, 20, 30, 40] — order preserved even if later items finish first
```

How to explain it in an interview:

1. Copy inputs into `items` so you can index them.
2. Track `next` (next index to start), `running` (in-flight count), `done` (finished count).
3. While `running < limit`, start another `iteratee`.
4. When one finishes, start the next. Store the result at the **original index**.
5. First rejection stops the pool (`stopped = true`). Already-started tasks may still finish; you just ignore further `resolve`.

Related: **Polyfill 9 (parallelLimit)** is the same pool without collecting mapped values — it only runs side-effect tasks.

---

## Polyfill 5 — Debounce

```javascript
function debounce(fn, wait, options = {}) {
  const { leading = false, trailing = true, maxWait } = options;
  let timer = null;
  let lastArgs = null;
  let lastThis = null;
  let lastCallTime = 0;
  let lastInvokeTime = 0;
  let result;

  function invoke() {
    const args = lastArgs;
    const ctx = lastThis;
    lastArgs = lastThis = null;
    lastInvokeTime = Date.now();
    result = fn.apply(ctx, args);
    return result;
  }

  function startTimer(remaining) {
    timer = setTimeout(() => {
      timer = null;
      if (trailing && lastArgs) {
        invoke();
      } else {
        lastArgs = lastThis = null;
      }
    }, remaining);
  }

  function cancel() {
    if (timer) clearTimeout(timer);
    timer = null;
    lastArgs = lastThis = null;
    lastCallTime = lastInvokeTime = 0;
  }

  function flush() {
    if (timer && lastArgs) {
      clearTimeout(timer);
      timer = null;
      return invoke();
    }
    return result;
  }

  function debounced(...args) {
    const now = Date.now();
    const isFirst = lastCallTime === 0;
    lastCallTime = now;
    lastArgs = args;
    lastThis = this;

    if (isFirst && leading) {
      invoke();
    }

    const timeSinceInvoke = now - lastInvokeTime;
    if (maxWait != null && timeSinceInvoke >= maxWait && lastArgs) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      invoke();
    }

    if (timer) clearTimeout(timer);
    startTimer(wait);
    return result;
  }

  debounced.cancel = cancel;
  debounced.flush = flush;
  return debounced;
}

const onType = debounce((q) => console.log("search", q), 300);
onType("a");
onType("ap");
onType("app");
// after 300ms of silence → "search app"

const onClick = debounce(save, 1000, { leading: true, trailing: false });
// first click runs immediately; later clicks in that 1s window are ignored
```

Interview variants:

- **Trailing (default):** wait for silence, then run once.
- **Leading:** run immediately, then ignore until `wait` has passed without calls.
- **Both:** first call runs now; last call in the burst also runs after silence.
- `cancel` / `flush` are often asked as follow-ups (Lodash API).

---

## Polyfill 6 — Throttle

```javascript
function throttle(fn, wait, options = {}) {
  const { leading = true, trailing = true } = options;
  let lastInvoke = 0;
  let timer = null;
  let lastArgs = null;
  let lastThis = null;

  function invoke(now) {
    lastInvoke = now;
    const args = lastArgs;
    const ctx = lastThis;
    lastArgs = lastThis = null;
    return fn.apply(ctx, args);
  }

  function throttled(...args) {
    const now = Date.now();
    if (!lastInvoke && !leading) {
      lastInvoke = now;
    }

    const remaining = wait - (now - lastInvoke);
    lastArgs = args;
    lastThis = this;

    if (remaining <= 0 || remaining > wait) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      invoke(now);
    } else if (!timer && trailing) {
      timer = setTimeout(() => {
        timer = null;
        invoke(leading ? Date.now() : 0);
      }, remaining);
    }
  }

  throttled.cancel = function () {
    if (timer) clearTimeout(timer);
    timer = null;
    lastArgs = lastThis = null;
    lastInvoke = 0;
  };

  return throttled;
}

window.addEventListener(
  "scroll",
  throttle(() => {
    console.log(window.scrollY);
  }, 200)
);
```

Difference vs debounce, in one sentence:

- **Throttle:** “At most once per window while events keep coming.”
- **Debounce:** “Only after the events have stopped.”

---

## Polyfill 7 — Event emitter

A tiny pub/sub object: `on`, `once`, `off`, `emit`. This is how Node’s `EventEmitter` and many UI buses work.

```javascript
class EventEmitter {
  constructor() {
    this.events = new Map();
  }

  on(event, listener) {
    if (typeof listener !== "function") {
      throw new TypeError("listener must be a function");
    }
    if (!this.events.has(event)) {
      this.events.set(event, []);
    }
    this.events.get(event).push(listener);
    return this;
  }

  once(event, listener) {
    const wrapped = (...args) => {
      this.off(event, wrapped);
      listener.apply(this, args);
    };
    wrapped._original = listener;
    return this.on(event, wrapped);
  }

  off(event, listener) {
    const list = this.events.get(event);
    if (!list) return this;

    this.events.set(
      event,
      list.filter((fn) => fn !== listener && fn._original !== listener)
    );
    return this;
  }

  emit(event, ...args) {
    const list = this.events.get(event);
    if (!list || list.length === 0) return false;

    // Copy so a listener can off() itself without skipping the next one
    [...list].forEach((fn) => {
      fn.apply(this, args);
    });
    return true;
  }

  listenerCount(event) {
    return (this.events.get(event) || []).length;
  }

  removeAllListeners(event) {
    if (event === undefined) {
      this.events.clear();
    } else {
      this.events.delete(event);
    }
    return this;
  }
}

const bus = new EventEmitter();

function onLogin(user) {
  console.log("welcome", user);
}

bus.on("login", onLogin);
bus.once("login", () => console.log("first login only"));
bus.emit("login", "Asha");
bus.emit("login", "Asha");
bus.off("login", onLogin);
```

Interview notes:

- Store listeners in a `Map` of arrays.
- `emit` must copy the array first, or `off` during emit skips neighbors.
- `once` wraps the listener and removes the wrapper after one call.
- `off(fn)` should also remove a `once` wrapper that points at `fn`.

---

## Polyfill 8 — `setInterval` polyfill

Do **not** implement interval as a recursive `setTimeout` that ignores drift. A better polyfill still uses `setTimeout`, but you can also explain the native difference: `setInterval` schedules on a fixed cadence; a naive recursive timeout waits `delay` **after** the callback finishes.

```javascript
const intervalStore = new Map();
let nextId = 1;

function mySetInterval(callback, delay, ...args) {
  const id = nextId++;
  let expected = Date.now() + delay;

  function tick() {
    if (!intervalStore.has(id)) return;

    callback.apply(undefined, args);

    if (!intervalStore.has(id)) return;

    expected += delay;
    const drift = Date.now() - expected;
    const nextWait = Math.max(0, delay - drift);
    const handle = setTimeout(tick, nextWait);
    intervalStore.set(id, handle);
  }

  intervalStore.set(id, setTimeout(tick, delay));
  return id;
}

function myClearInterval(id) {
  const handle = intervalStore.get(id);
  if (handle != null) {
    clearTimeout(handle);
    intervalStore.delete(id);
  }
}

const id = mySetInterval(() => {
  console.log("tick", Date.now());
}, 1000);

setTimeout(() => myClearInterval(id), 4500);
```

Simpler interview version (good enough if they want the idea fast):

```javascript
function simpleSetInterval(fn, delay) {
  let stopped = false;

  function loop() {
    if (stopped) return;
    fn();
    setTimeout(loop, delay);
  }

  setTimeout(loop, delay);
  return () => {
    stopped = true;
  };
}

const stop = simpleSetInterval(() => console.log("hi"), 1000);
// stop();
```

Caveats to mention:

- If `fn` takes longer than `delay`, native `setInterval` can **stack** callbacks (browser) or skip (some engines). Recursive `setTimeout` never overlaps.
- Always return an id / cancel function. Memory-leak question: forgotten intervals.

---

## Polyfill 9 — Parallel limit function

Run a list of async tasks with a concurrency cap. Unlike `mapLimit`, you often only care that they **all finish**, not a mapped array. This version supports both styles.

```javascript
function parallelLimit(tasks, limit) {
  return mapLimit(tasks, limit, (task) => task());
}

// Or standalone, without mapLimit:
function parallelLimitStandalone(tasks, limit) {
  const list = Array.from(tasks);

  return new Promise((resolve, reject) => {
    const results = new Array(list.length);
    let next = 0;
    let running = 0;
    let completed = 0;
    let failed = false;

    if (list.length === 0) {
      resolve([]);
      return;
    }

    const runNext = () => {
      while (running < limit && next < list.length && !failed) {
        const index = next;
        const task = list[index];
        next += 1;
        running += 1;

        Promise.resolve()
          .then(() => task())
          .then((value) => {
            results[index] = value;
            running -= 1;
            completed += 1;
            if (completed === list.length) resolve(results);
            else runNext();
          })
          .catch((err) => {
            failed = true;
            reject(err);
          });
      }
    };

    runNext();
  });
}

const tasks = [
  () => fetch("/a").then((r) => r.json()),
  () => fetch("/b").then((r) => r.json()),
  () => fetch("/c").then((r) => r.json()),
];

parallelLimit(tasks, 2).then(console.log);
```

`Promise.all(tasks.map(t => t()))` is **unlimited** parallel. `parallelLimit` is the controlled version.

---

## Polyfill 10 — Deep vs shallow copy

### Shallow copy

Only the **first level** is new. Nested objects are still shared.

```javascript
function shallowCopy(value) {
  if (Array.isArray(value)) {
    return value.slice();
  }
  if (value && typeof value === "object") {
    return Object.assign({}, value);
    // or { ...value }
  }
  return value;
}

const original = { name: "Asha", address: { city: "Pune" } };
const shallow = shallowCopy(original);
shallow.name = "Dev";
shallow.address.city = "Mumbai";

console.log(original.name);          // "Asha" — top-level is independent
console.log(original.address.city);  // "Mumbai" — nested object was shared
```

### Deep copy

Every nested plain object / array is cloned.

```javascript
function deepCopy(value, seen = new WeakMap()) {
  if (value === null || typeof value !== "object") {
    return value;
  }

  if (seen.has(value)) {
    return seen.get(value);
  }

  if (value instanceof Date) {
    return new Date(value.getTime());
  }

  if (value instanceof RegExp) {
    return new RegExp(value.source, value.flags);
  }

  if (value instanceof Map) {
    const copy = new Map();
    seen.set(value, copy);
    value.forEach((v, k) => {
      copy.set(deepCopy(k, seen), deepCopy(v, seen));
    });
    return copy;
  }

  if (value instanceof Set) {
    const copy = new Set();
    seen.set(value, copy);
    value.forEach((v) => copy.add(deepCopy(v, seen)));
    return copy;
  }

  if (Array.isArray(value)) {
    const copy = [];
    seen.set(value, copy);
    for (let i = 0; i < value.length; i++) {
      copy[i] = deepCopy(value[i], seen);
    }
    return copy;
  }

  const copy = Object.create(Object.getPrototypeOf(value));
  seen.set(value, copy);

  for (const key of Reflect.ownKeys(value)) {
    const desc = Object.getOwnPropertyDescriptor(value, key);
    if (desc.get || desc.set) {
      Object.defineProperty(copy, key, desc);
    } else {
      desc.value = deepCopy(desc.value, seen);
      Object.defineProperty(copy, key, desc);
    }
  }

  return copy;
}

const nested = { a: { b: 1 }, list: [2, { c: 3 }] };
nested.self = nested; // circular

const clone = deepCopy(nested);
clone.a.b = 99;
console.log(nested.a.b); // 1
console.log(clone.self === clone); // true
console.log(clone.self !== nested); // true
```

Built-in options to mention:

```javascript
structuredClone(value); // best modern API (dates, maps, circular) — no functions
JSON.parse(JSON.stringify(value)); // loses undefined, functions, dates, maps, symbols
```

**Interview takeaway**

Shallow = new wrapper, same children. Deep = new tree. `WeakMap` is how you survive circular references.

---

## Polyfill 11 — Flatten a deeply nested object

Turn `{ a: { b: { c: 1 } }, d: [2, 3] }` into a single-level object with path keys.

```javascript
function flattenObject(input, options = {}) {
  const { delimiter = ".", prefix = "" } = options;
  const result = {};

  function walk(value, path) {
    const isObject =
      value !== null &&
      typeof value === "object" &&
      !(value instanceof Date) &&
      !(value instanceof RegExp);

    if (!isObject) {
      result[path] = value;
      return;
    }

    const keys = Array.isArray(value)
      ? value.map((_, i) => i)
      : Object.keys(value);

    if (keys.length === 0) {
      result[path] = Array.isArray(value) ? [] : {};
      return;
    }

    keys.forEach((key) => {
      const nextPath = path ? `${path}${delimiter}${key}` : String(key);
      walk(value[key], nextPath);
    });
  }

  if (input === null || typeof input !== "object") {
    return input;
  }

  walk(input, prefix);
  return result;
}

const nested = {
  user: {
    name: "Asha",
    address: { city: "Pune", pin: 411001 },
  },
  tags: ["js", "node"],
};

console.log(flattenObject(nested));
// {
//   "user.name": "Asha",
//   "user.address.city": "Pune",
//   "user.address.pin": 411001,
//   "tags.0": "js",
//   "tags.1": "node"
// }
```

Unflatten (often asked as a follow-up):

```javascript
function unflattenObject(flat, delimiter = ".") {
  const result = {};

  Object.keys(flat).forEach((path) => {
    const parts = path.split(delimiter);
    let cursor = result;

    parts.forEach((part, i) => {
      const isLast = i === parts.length - 1;
      const nextIsIndex = !isLast && /^\d+$/.test(parts[i + 1]);

      if (isLast) {
        cursor[part] = flat[path];
      } else {
        if (cursor[part] == null) {
          cursor[part] = nextIsIndex ? [] : {};
        }
        cursor = cursor[part];
      }
    });
  });

  return result;
}

console.log(unflattenObject(flattenObject(nested)));
```

Flatten an **array** (different question — also common):

```javascript
function flattenArray(arr, depth = Infinity) {
  const out = [];

  for (const item of arr) {
    if (Array.isArray(item) && depth > 0) {
      out.push(...flattenArray(item, depth - 1));
    } else {
      out.push(item);
    }
  }
  return out;
}

console.log(flattenArray([1, [2, [3, [4]]]], 2)); // [1, 2, 3, [4]]
```

Native: `arr.flat(depth)`.

---

## Polyfill 12 — Memoization / caching

```javascript
function defaultKey(args) {
  return args
    .map((arg) => {
      const type = typeof arg;
      if (arg === null) return "null";
      if (type === "object" || type === "function") {
        throw new Error("Pass a key resolver for object/function arguments");
      }
      return type + ":" + String(arg);
    })
    .join("|");
}

function memoize(fn, resolver) {
  const cache = new Map();
  const pending = new Map(); // optional: collapse in-flight async calls

  function memoized(...args) {
    const key = resolver ? resolver(...args) : defaultKey(args);

    if (cache.has(key)) {
      return cache.get(key);
    }

    if (pending.has(key)) {
      return pending.get(key);
    }

    const value = fn.apply(this, args);

    if (value && typeof value.then === "function") {
      const p = Promise.resolve(value)
        .then((result) => {
          cache.set(key, result);
          pending.delete(key);
          return result;
        })
        .catch((err) => {
          pending.delete(key);
          throw err;
        });
      pending.set(key, p);
      return p;
    }

    cache.set(key, value);
    return value;
  }

  memoized.cache = cache;
  memoized.clear = () => {
    cache.clear();
    pending.clear();
  };
  memoized.delete = (key) => {
    cache.delete(key);
    pending.delete(key);
  };

  return memoized;
}

function expensive(n) {
  console.log("compute", n);
  return n * n;
}

const cached = memoize(expensive);
cached(4); // compute 4
cached(4); // cache hit, no log

const fetchUser = memoize(
  (id) => fetch("/users/" + id).then((r) => r.json()),
  (id) => "user:" + id
);
```

LRU cache variant (common follow-up):

```javascript
function memoizeLru(fn, maxSize = 100, resolver) {
  const cache = new Map(); // insertion order = recency if we re-set on hit

  return function (...args) {
    const key = resolver ? resolver(...args) : defaultKey(args);

    if (cache.has(key)) {
      const value = cache.get(key);
      cache.delete(key);
      cache.set(key, value); // move to most-recent
      return value;
    }

    const value = fn.apply(this, args);
    cache.set(key, value);

    if (cache.size > maxSize) {
      const oldest = cache.keys().next().value;
      cache.delete(oldest);
    }
    return value;
  };
}
```

**Interview takeaway**

Cache only **pure** work. For objects, never use `JSON.stringify` blindly. For async, decide: cache the promise (dedupe in-flight) or cache only success.

---

## Polyfill 13 — `Promise.prototype.finally`

`finally` runs when the promise settles, **does not change** the fulfillment value, and **re-throws** the rejection unless `onFinally` itself fails.

```javascript
if (!Promise.prototype.finally) {
  Promise.prototype.finally = function (onFinally) {
    return this.then(
      (value) => Promise.resolve(onFinally()).then(() => value),
      (reason) =>
        Promise.resolve(onFinally()).then(() => {
          throw reason;
        })
    );
  };
}

// Standalone helper (does not patch the prototype)
function promiseFinally(promise, onFinally) {
  return Promise.resolve(promise).then(
    (value) => Promise.resolve(onFinally()).then(() => value),
    (reason) =>
      Promise.resolve(onFinally()).then(() => {
        throw reason;
      })
  );
}

Promise.resolve(10)
  .finally(() => console.log("cleanup"))
  .then((n) => console.log(n)); // cleanup, then 10

Promise.reject(new Error("x"))
  .finally(() => console.log("cleanup"))
  .catch((err) => console.log(err.message)); // cleanup, then x
```

Why `Promise.resolve(onFinally())`:

- `onFinally` may return a promise (async cleanup). Wait for it.
- If cleanup **rejects**, that error wins (it hides the original value/reason).
- If cleanup **fulfills**, pass through the original value or reason.

```javascript
Promise.resolve("keep me")
  .finally(() => {
    throw new Error("cleanup failed");
  })
  .catch((err) => console.log(err.message)); // "cleanup failed"
```

---

## Polyfill 14 — Retry

Retry an async function when it fails, with optional delay and backoff.

```javascript
function retry(fn, options = {}) {
  const {
    retries = 3,
    delay = 0,
    factor = 1,
    shouldRetry = () => true,
    onRetry,
  } = options;

  return function retried(...args) {
    let attempt = 0;

    const run = () =>
      Promise.resolve()
        .then(() => fn.apply(this, args))
        .catch((err) => {
          const canRetry = attempt < retries && shouldRetry(err, attempt);
          if (!canRetry) {
            throw err;
          }

          const wait = delay * Math.pow(factor, attempt);
          attempt += 1;

          if (onRetry) {
            onRetry(err, attempt, wait);
          }

          if (wait <= 0) {
            return run();
          }

          return new Promise((resolve) => setTimeout(resolve, wait)).then(run);
        });

    return run();
  };
}

async function flakyFetch(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error("HTTP " + res.status);
  }
  return res.json();
}

const fetchWithRetry = retry(flakyFetch, {
  retries: 3,
  delay: 200,
  factor: 2, // 200, 400, 800
  shouldRetry: (err) => err.message !== "HTTP 404",
  onRetry: (err, attempt, wait) => {
    console.log(`retry #${attempt} after ${wait}ms`, err.message);
  },
});

fetchWithRetry("/api/user").then(console.log).catch(console.error);
```

Immediate helper (no wrapper function):

```javascript
async function retryAsync(fn, retries = 3, delay = 0) {
  let lastError;

  for (let i = 0; i <= retries; i++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      if (i === retries) break;
      if (delay) {
        await new Promise((r) => setTimeout(r, delay));
      }
    }
  }

  throw lastError;
}

await retryAsync(() => fetch("/api").then((r) => r.json()), 2, 100);
```

Interview notes:

- `retries: 3` usually means **3 extra attempts** (4 total). Say which one you chose.
- Do not retry 404 / validation errors. Retry 429 / 503 / network failures.
- Exponential backoff + jitter (`delay * 2^n + random`) avoids thundering herds.
- Combine with **timeout** (`Promise.race` vs a timer) if they ask a follow-up.

---

# Quick revision sheet

Use this the night before the interview.

| Topic | One-line answer |
|---|---|
| Scope | Where a name is visible: global / function / block |
| Scope chain | Lookup walks outer lexical scopes |
| Primitive vs ref | Copy value vs copy pointer |
| var / let / const | function vs block; const cannot rebind |
| TDZ | `let`/`const` exist but are unreadable until init |
| Hoisting | Creation phase registers bindings |
| Prototype | Shared object used for property lookup |
| Prototype chain | `obj → proto → … → null` |
| Closure | Function + remembered lexical env |
| Pass style | Always pass-by-value; objects copy the reference |
| Curry | `f(a)(b)(c)` from `f(a,b,c)` |
| Infinite curry | Need a stop: `()` or `valueOf` |
| Memoize | Cache pure results by argument key |
| Rest / spread | Collect vs expand |
| Generators | `function*` / `yield` pause-resume iterator |
| Single thread | One JS stack; async via event loop |
| Event loop | Stack empty → microtasks → (paint) → next macrotask |
| Promise | Later value; `then` is a microtask |
| Bubbling / capture | Up vs down the DOM |
| Delegation | One parent listener + `target` |
| Throttle / debounce | Heartbeat vs wait-for-silence |
| `this` | How you call it; arrows are lexical |
| `===` vs `==` | No coerce vs coerce |
| Lexical env | Variable map + outer pointer |

| Polyfill | Core trick |
|---|---|
| map / filter / … | Loop + `callback.call(thisArg, item, i, arr)` |
| call / apply | Temp property on `thisArg`, then invoke |
| bind | Return a function; honor `new` |
| Promise.all | Count down remaining; reject on first error |
| Promise.race | First settle wins |
| Promise.any | First fulfill wins; all reject → `AggregateError` |
| Promise.allSettled | Never fail for children; `{status, value\|reason}` |
| mapLimit / parallelLimit | Worker pool: start next when one finishes |
| debounce / throttle | `setTimeout` + last-args / last-time |
| EventEmitter | `Map<event, fn[]>` |
| setInterval | Recursive `setTimeout` + cancel id |
| deepCopy | Recurse + `WeakMap` for cycles |
| flatten object | Recurse paths `a.b.c` |
| finally | Wait for cleanup, then pass original value/error |
| retry | Loop on reject with delay / backoff |

---

# Suggested practice order (2–3 days)

**Day 1 — Language core**  
Q1–11, Q41, Q43, Q46–47, Q49–54, Q52–53.

**Day 2 — Functions and async**  
Q12–16, Q18, Q20–24, Q34–40, Q44–45. Then Promise polyfills.

**Day 3 — DOM + utilities + polyfills**  
Q25–33, Q38, Q48. Then map/reduce, call/bind, debounce/throttle, mapLimit, deep copy, retry.

For each polyfill: hide this file, write it in an empty editor, then compare.

Good luck. Speak the “interview takeaway” line first, then open the code if they want depth.
