// A tiny C++ target for memscout's end-to-end tests.
//
// It heap-allocates a known number of polymorphic Widget objects with known
// field values, prints each object's address and fields as ground truth, then
// parks in pause() so the test can attach with memscout, scan for the Widget
// vtable, decode the fields, and check them against what this program reported.
//
// The layout is deliberately fixed and documented so the test can use explicit
// field offsets (x86-64, Itanium C++ ABI):
//
//   offset  field         type
//        0  <vptr>        (pointer to Widget's vtable + 16)
//        8  mActive       bool
//        9  mHidden       bool
//       12  mId           int32_t
//       16  mValue        uint64_t
//       24  mData         const char*   (ns[C]String-style: data pointer ...)
//       32  mLength       uint32_t      (... then length -> "24:nscstring:...")
//
// Build with symbols kept (no strip) so the local symbol table carries
// _ZTV6Widget. The out-of-line destructor is the vtable's key function, which
// forces a strong vtable definition in this translation unit.

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <unistd.h>

class Widget {
 public:
  virtual ~Widget();
  virtual int kind();
  bool mActive;
  bool mHidden;
  int32_t mId;
  uint64_t mValue;
  const char* mData;
  uint32_t mLength;
};

Widget::~Widget() {}
int Widget::kind() { return 1; }

int main() {
  static const char* kNames[] = {"alpha", "bravo", "charlie"};
  const int kCount = 3;

  for (int i = 0; i < kCount; ++i) {
    Widget* w = new Widget();
    w->mActive = (i % 2 == 0);
    w->mHidden = (i == 1);
    w->mId = 100 + i;
    w->mValue = 0xF00D0000ULL + i;
    w->mData = kNames[i];
    w->mLength = (uint32_t)strlen(kNames[i]);

    // Ground-truth line the test parses and compares memscout's decode against.
    printf("OBJ %p active=%d hidden=%d id=%d value=%llu name=%s\n",
           (void*)w, w->mActive ? 1 : 0, w->mHidden ? 1 : 0, w->mId,
           (unsigned long long)w->mValue, w->mData);
  }

  printf("READY %d\n", (int)getpid());
  fflush(stdout);

  pause();  // wait to be terminated by the test harness
  return 0;
}
