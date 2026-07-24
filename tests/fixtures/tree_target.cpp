// A target that builds a small binary tree of Node objects, prints its structure as
// ground truth, and parks -- so a test can traverse the tree in live memory by
// following child pointers with memscout.
//
// Node layout (x86-64, Itanium C++ ABI):
//   offset  field     type
//        0  <vptr>
//        8  mId       int32_t
//       16  mLeft     Node*
//       24  mRight    Node*
//
// The tree:            1
//                    /   \
//                   2     3
//                  /     / \
//                 4     5   6

#include <cstdint>
#include <cstdio>
#include <unistd.h>

class Node {
 public:
  explicit Node(int id) : mId(id), mLeft(nullptr), mRight(nullptr) {}
  virtual ~Node();
  virtual int kind();
  int32_t mId;
  Node* mLeft;
  Node* mRight;
};

Node::~Node() {}
int Node::kind() { return 1; }

int main() {
  Node* n1 = new Node(1);
  Node* n2 = new Node(2);
  Node* n3 = new Node(3);
  Node* n4 = new Node(4);
  Node* n5 = new Node(5);
  Node* n6 = new Node(6);
  n1->mLeft = n2;  n1->mRight = n3;
  n2->mLeft = n4;
  n3->mLeft = n5;  n3->mRight = n6;

  Node* all[] = {n1, n2, n3, n4, n5, n6};
  for (Node* n : all) {
    printf("NODE %p id=%d left=%p right=%p\n",
           (void*)n, n->mId, (void*)n->mLeft, (void*)n->mRight);
  }
  printf("ROOT %p\n", (void*)n1);
  printf("READY pid=%d\n", (int)getpid());
  fflush(stdout);

  pause();  // stay alive so the test can walk the tree
  return 0;
}
